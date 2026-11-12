"""Bounded native HTTP/SSE client using Home Assistant's shared session."""

import asyncio
import codecs
import json
import math
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout
from httpx_sse._decoders import SSEDecoder, SSELineDecoder
from httpx_sse._models import ServerSentEvent
from packaging.version import InvalidVersion, Version
from yarl import URL

from .const import (
    AUTH_HEADER,
    COVER_ART_ARCHIVE_URL,
    MAX_EVENT_CHARS,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_CACHE_BYTES,
    MAX_JSON_BYTES,
    MAX_LYRICS_BYTES,
    MIN_SERVER_VERSION,
    REQUEST_TIMEOUT,
    WATCHDOG,
)
from .models import Session, User, number, text


class NavidromeError(Exception):
    """A sanitized protocol or transport failure."""


class CannotConnect(NavidromeError):
    """Network, TLS, timeout or server failure."""


class InvalidAuth(NavidromeError):
    """Credentials or tokens have been rejected."""


class NotAdmin(NavidromeError):
    """All-user monitoring requires an administrator."""


class NotSupported(NavidromeError):
    """Wrong server/version or unusable SSE/proxy response."""


class ProtocolError(NavidromeError):
    """Malformed or oversized server response."""


def normalize_url(value: str) -> str:
    """Preserve BasePath; reject embedded credentials, queries and fragments."""
    try:
        url = URL(value.strip())
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.user is not None
            or url.query_string
            or url.fragment
            or any(part in {".", ".."} for part in url.parts)
        ):
            raise ValueError
        return str(url).rstrip("/")
    except (ValueError, TypeError):
        raise ValueError("Enter an HTTP(S) server base URL without credentials or query") from None


async def bounded_body(response: ClientResponse, limit: int) -> bytes:
    """Bound decoded bytes, including chunked/compressed responses."""
    body = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        body.extend(chunk)
        if len(body) > limit:
            raise ProtocolError("Server response exceeds size limit")
    return bytes(body)


async def decode_events(response: ClientResponse) -> AsyncIterator[ServerSentEvent]:
    """Adapt the pinned library's incremental decoders, with memory bounds."""
    utf8 = codecs.getincrementaldecoder("utf-8-sig")("strict")
    lines = SSELineDecoder()
    events = SSEDecoder()
    frame_size = 0
    try:
        async for chunk in response.content.iter_chunked(4096):
            decoded = utf8.decode(chunk)
            for line in lines.decode(decoded):
                frame_size += len(line) + 1
                if frame_size > MAX_EVENT_CHARS:
                    raise ProtocolError("SSE frame exceeds size limit")
                event = events.decode(line)
                if not line:
                    frame_size = 0
                if event is not None:
                    yield event
            if sum(map(len, lines.buffer)) + frame_size > MAX_EVENT_CHARS:
                raise ProtocolError("SSE line exceeds size limit")
        utf8.decode(b"", final=True)
    except UnicodeError:
        raise ProtocolError("Invalid SSE UTF-8") from None
    raise CannotConnect("SSE stream closed; a fresh snapshot is required")


class NavidromeClient:
    """One connection's credentials, tokens, HTTP requests and image cache."""

    def __init__(self, session: ClientSession, url: str, username: str, password: str) -> None:
        self._session = session
        self.url = normalize_url(url)
        self.username = username
        self._password = password
        self._token = ""
        self._salt = ""
        self._subsonic_token = ""
        self._login_lock = asyncio.Lock()
        self._generation = 0
        self.version: str | None = None
        self._images: OrderedDict[tuple[str, int], tuple[float, bytes, str]] = OrderedDict()

    def _url(self, path: str) -> str:
        return self.url + "/" + path

    def _refresh_token(self, response: ClientResponse) -> None:
        if token := response.headers.get(AUTH_HEADER):
            self._token = token.removeprefix("Bearer ")

    @staticmethod
    def _check_status(response: ClientResponse) -> None:
        if response.status == 401:
            raise InvalidAuth("Authentication rejected (HTTP 401)")
        if response.status == 403:
            raise NotAdmin("Administrator access required (HTTP 403)")
        if 300 <= response.status < 500:
            raise NotSupported(
                f"Endpoint missing or redirected (HTTP {response.status}); "
                "check server URL and proxy"
            )
        if response.status != 200:
            raise CannotConnect(f"Server request failed (HTTP {response.status})")

    async def _json(self, response: ClientResponse) -> Any:
        if response.content_type != "application/json":
            raise NotSupported("Expected JSON; check server base path and authentication proxy")
        try:
            return json.loads(await bounded_body(response, MAX_JSON_BYTES))
        except (ValueError, UnicodeError):
            raise ProtocolError("Invalid server JSON") from None

    async def login(self) -> None:
        """Authenticate once; never retain account internals."""
        try:
            async with self._session.post(
                self._url("auth/login"),
                json={"username": self.username, "password": self._password},
                allow_redirects=False,
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                self._check_status(response)
                data = await self._json(response)
                if not isinstance(data, dict):
                    raise ProtocolError("Invalid login response")
                if data.get("isAdmin") is not True:
                    raise NotAdmin("Administrator access required")
                fields = [data.get(key) for key in ("token", "subsonicSalt", "subsonicToken")]
                if not all(isinstance(value, str) and value for value in fields):
                    raise ProtocolError("Login omitted native or Subsonic tokens")
                self._token, self._salt, self._subsonic_token = fields
                self._refresh_token(response)
                self._generation += 1
                self._images.clear()
        except (ClientError, TimeoutError):
            raise CannotConnect("Cannot connect securely to Navidrome") from None

    async def _ensure_login(self, generation: int) -> None:
        async with self._login_lock:
            if not self._token or self._generation == generation:
                await self.login()

    def _rest_params(self) -> dict[str, str]:
        return {
            "u": self.username,
            "t": self._subsonic_token,
            "s": self._salt,
            "v": "1.16.1",
            "c": "homeassistant-navidrome",
            "f": "json",
        }

    @asynccontextmanager
    async def _request(
        self, path: str, *, rest: bool = False, params: dict | None = None, stream: bool = False
    ) -> AsyncIterator[ClientResponse]:
        if not self._token:
            await self._ensure_login(self._generation)
        for attempt in range(2):
            generation = self._generation
            query = (self._rest_params() if rest else {}) | (params or {})
            delivered = False
            try:
                async with self._session.get(
                    self._url(path),
                    params=query,
                    headers={
                        AUTH_HEADER: f"Bearer {self._token}",
                        "Accept": ("text/event-stream" if stream else "application/json"),
                    },
                    allow_redirects=False,
                    timeout=ClientTimeout(
                        total=None if stream else REQUEST_TIMEOUT,
                        sock_connect=10,
                        sock_read=WATCHDOG if stream else REQUEST_TIMEOUT,
                    ),
                ) as response:
                    self._check_status(response)
                    self._refresh_token(response)
                    delivered = True
                    yield response
                    return
            except InvalidAuth:
                if delivered or attempt:
                    raise
                await self._ensure_login(generation)
            except (ClientError, TimeoutError):
                raise CannotConnect("Connection lost or timed out") from None

    async def users(self) -> dict[str, User]:
        """Read the entire native paginated admin roster atomically."""
        users: dict[str, User] = {}
        offset = 0
        while offset < 100000:
            async with self._request(
                "api/user", params={"_start": offset, "_end": offset + 100, "_sort": "id"}
            ) as response:
                data = await self._json(response)
                total = response.headers.get("x-total-count")
            if not isinstance(data, list):
                raise ProtocolError("Invalid user roster")
            previous_count = len(users)
            for item in data:
                if not isinstance(item, dict):
                    raise ProtocolError("Invalid user record")
                user_id, username = item.get("id"), item.get("userName")
                if (
                    not isinstance(user_id, str)
                    or not user_id
                    or not isinstance(username, str)
                    or not username
                    or user_id in users
                ):
                    raise ProtocolError("Missing native user identity")
                users[user_id] = User(user_id, username)
            if not data:
                if total is not None and (not total.isdigit() or offset < int(total)):
                    raise ProtocolError("Incomplete user roster")
                return users
            if len(users) == previous_count:
                raise ProtocolError("User pagination made no progress")
            offset += len(data)
            if total is not None:
                if not total.isdigit():
                    raise ProtocolError("Invalid roster total")
                if offset >= int(total):
                    return users
            elif len(data) < 100:
                return users
        raise ProtocolError("User roster exceeds size limit")

    async def now_playing(self) -> list[Session]:
        """HTTP success alone is not Subsonic success."""
        async with self._request("rest/getNowPlaying.view", rest=True) as response:
            data = await self._json(response)
            if not isinstance(data, dict) or not isinstance(data.get("subsonic-response"), dict):
                raise ProtocolError("Missing Subsonic response")
            root = data["subsonic-response"]
            if root.get("status") != "ok":
                error = root.get("error", {})
                if isinstance(error, dict) and error.get("code") in {40, 41}:
                    raise InvalidAuth("Subsonic credentials rejected")
                raise ProtocolError("Subsonic request failed")
            if root.get("type") != "navidrome":
                raise NotSupported("This integration requires Navidrome")
            now_playing = root.get("nowPlaying")
            if not isinstance(now_playing, dict):
                raise ProtocolError("Missing now-playing snapshot")
            entries = now_playing.get("entry", [])
            if entries is None:
                entries = []
            if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
                raise ProtocolError("Invalid snapshot entries")
            try:
                return [Session.from_json(item) for item in entries]
            except ValueError:
                raise ProtocolError("Invalid snapshot entry") from None

    async def static_cover(self, album_id: str) -> str | None:
        """Resolve a release's public front-cover URL without sending Navidrome credentials."""
        async with self._request("rest/getAlbum", rest=True, params={"id": album_id}) as response:
            data = await self._json(response)
        root = data.get("subsonic-response") if isinstance(data, dict) else None
        if not isinstance(root, dict):
            raise ProtocolError("Missing album Subsonic response")
        if root.get("status") != "ok":
            error = root.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if code == 40 or code == 41:
                raise InvalidAuth("Album credentials rejected")
            if code == 70:
                return None
            raise ProtocolError("Album lookup failed")
        album = root.get("album")
        if not isinstance(album, dict):
            raise ProtocolError("Invalid album metadata")
        if not (release_id := text(album.get("musicBrainzId"))):
            return None
        try:
            release = UUID(release_id)
        except ValueError:
            return None
        if not release.int:
            return None
        url = f"{COVER_ART_ARCHIVE_URL}/release/{release}/front"
        try:
            async with self._session.head(
                url,
                headers={"Accept": "image/*", "User-Agent": "HomeAssistant-Navidrome"},
                allow_redirects=False,
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                if response.status == 404:
                    return None
                if response.status in {301, 302, 303, 307, 308}:
                    if not response.headers.get("Location"):
                        raise ProtocolError("Cover Art Archive redirect omitted image location")
                elif response.status != 200:
                    raise CannotConnect(f"Cover Art Archive returned HTTP {response.status}")
                elif not response.content_type.startswith("image/"):
                    raise ProtocolError("Cover Art Archive did not return an image")
        except (ClientError, TimeoutError):
            raise CannotConnect("Cannot connect to Cover Art Archive") from None
        return url

    async def lyrics(self, song_id: str) -> dict[str, Any] | None:
        """Prefer synced lyrics, retaining only bounded, validated display data."""
        async with self._request(
            "rest/getLyricsBySongId", rest=True, params={"id": song_id}
        ) as response:
            data = await self._json(response)
        root = data.get("subsonic-response") if isinstance(data, dict) else None
        if not isinstance(root, dict):
            raise ProtocolError("Missing lyrics Subsonic response")
        if root.get("status") != "ok":
            error = root.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if code == 40 or code == 41:
                raise InvalidAuth("Lyrics credentials rejected")
            if code == 70:
                return None
            raise ProtocolError("Lyrics request failed")
        container = root.get("lyricsList")
        if container is None:
            return None
        if not isinstance(container, dict):
            raise ProtocolError("Invalid lyrics list")
        variants = container.get("structuredLyrics") or []
        if not isinstance(variants, list):
            raise ProtocolError("Invalid structured lyrics")
        candidates = []
        for variant in variants:
            if not isinstance(variant, dict) or not isinstance(variant.get("line", []), list):
                raise ProtocolError("Invalid lyrics variant")
            source = variant.get("line", [])
            if len(source) > 2000:
                raise ProtocolError("Lyrics exceed line limit")
            lines = []
            for line in source:
                if not isinstance(line, dict) or not isinstance(line.get("value"), str):
                    raise ProtocolError("Invalid lyrics line")
                lines.append({"value": line["value"], "start": number(line.get("start"))})
            if not any(line["value"].strip() for line in lines):
                continue
            synced = variant.get("synced") is True and all(
                line["start"] is not None for line in lines
            )
            if synced:
                lines.sort(key=lambda line: line["start"])
            else:
                lines = [{"value": line["value"]} for line in lines]
            result = {"synced": synced, "line": lines}
            for field in ("lang", "displayArtist", "displayTitle"):
                if value := text(variant.get(field)):
                    result[field] = value
            offset = variant.get("offset", 0)
            if (
                isinstance(offset, (int, float))
                and not isinstance(offset, bool)
                and math.isfinite(offset)
            ):
                result["offset"] = offset
            if len(json.dumps(result).encode()) > MAX_LYRICS_BYTES:
                raise ProtocolError("Lyrics exceed attribute size limit")
            candidates.append(result)
        return next(
            (candidate for candidate in candidates if candidate["synced"]),
            candidates[0] if candidates else None,
        )

    def check_server_start(self, event: ServerSentEvent) -> None:
        """Require a released source-compatible Navidrome version."""
        try:
            data = json.loads(event.data)
            version = data["version"]
            parsed = Version(version.partition(" (")[0].removeprefix("v"))
            if parsed < Version(MIN_SERVER_VERSION) or parsed.is_prerelease:
                raise NotSupported("Navidrome 0.64.2 or newer stable is required")
            self.version = version
        except (ValueError, TypeError, KeyError, AttributeError, InvalidVersion):
            raise NotSupported("Cannot identify Navidrome server version") from None

    @asynccontextmanager
    async def events(self) -> AsyncIterator[AsyncIterator[ServerSentEvent]]:
        """Own only this response, never the shared session. No replay assumption."""
        async with self._request("api/events", stream=True) as response:
            if response.content_type != "text/event-stream":
                raise NotSupported(
                    "Enable DevActivityPanel and disable proxy buffering/login redirects"
                )
            iterator = decode_events(response)
            try:
                yield iterator
            finally:
                await iterator.aclose()

    async def validate(self) -> None:
        """Read-only setup probe, never submit playback or change server config."""
        await self.login()
        await self.users()
        async with self.events() as events:
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    async for event in events:
                        if event.event == "serverStart":
                            self.check_server_start(event)
                            await self.now_playing()
                            return
            except TimeoutError:
                raise NotSupported("No serverStart received; check SSE proxy buffering") from None

    async def image(self, cover_id: str, size: int = 512) -> tuple[bytes, str]:
        """Fetch authenticated artwork for HA's media image proxy, with bounded LRU."""
        key = (cover_id, size)
        cached = self._images.get(key)
        if cached is not None and time.monotonic() - cached[0] < 3600:
            self._images.move_to_end(key)
            return cached[1], cached[2]
        async with self._request(
            "rest/getCoverArt", rest=True, params={"id": cover_id, "size": size}
        ) as response:
            content_type = response.content_type
            if content_type == "application/json":
                data = await self._json(response)
                root = data.get("subsonic-response") if isinstance(data, dict) else None
                error = root.get("error") if isinstance(root, dict) else None
                code = error.get("code") if isinstance(error, dict) else None
                if isinstance(code, int) and not isinstance(code, bool):
                    error_type = InvalidAuth if code in {40, 41} else ProtocolError
                    raise error_type(f"Cover art request failed (Subsonic code {code})")
                raise ProtocolError(
                    "Artwork endpoint returned application/json instead of an image"
                )
            if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise ProtocolError(
                    f"Artwork endpoint returned {content_type} instead of a supported image"
                )
            body = await bounded_body(response, MAX_IMAGE_BYTES)
        self._images[key] = (time.monotonic(), body, content_type)
        self._images.move_to_end(key)
        cached_bytes = sum(len(image[1]) for image in self._images.values())
        while len(self._images) > 32 or cached_bytes > MAX_IMAGE_CACHE_BYTES:
            _, evicted = self._images.popitem(last=False)
            cached_bytes -= len(evicted[1])
        return body, content_type
