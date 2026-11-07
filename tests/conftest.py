"""Reusable in-process Navidrome-shaped HTTP server, not a live-server claim."""

import asyncio
import json
from unittest.mock import patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.resolver import ThreadedResolver
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame


class LocalResolver(ThreadedResolver):
    """Supply the extra close hook used by HA's resolver ownership wrapper."""

    async def real_close(self):
        await super().close()


@pytest.fixture
async def hass(tmp_path):
    """A real HA core instance with a temporary config directory."""
    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)
    with patch(
        "homeassistant.helpers.aiohttp_client._async_make_resolver", return_value=LocalResolver()
    ):
        yield instance
        await instance.async_stop(force=True)


@pytest.fixture
async def server(aiohttp_server):
    """Serve the pinned source contract, including a BasePath and streaming frames."""
    state = {
        "entries": [],
        "users": [{"id": "user-a", "userName": "Alice"}, {"id": "user-b", "userName": "Bob"}],
        "login_count": 0,
        "requests": [],
        "streams": set(),
        "stream_type": "text/event-stream",
        "status": 200,
        "admin": True,
        "rest_error": None,
        "reject_native": 0,
        "version": "0.64.2 (source-fixture)",
    }

    async def login(request):
        state["login_count"] += 1
        assert await request.json() == {"username": "admin", "password": "secret"}
        return web.json_response(
            {
                "token": "jwt-initial",
                "isAdmin": state["admin"],
                "subsonicSalt": "salt",
                "subsonicToken": "salted-token",
            },
            status=state["status"],
        )

    async def authenticated(request):
        state["requests"].append((request.path, dict(request.query), dict(request.headers)))
        assert request.headers.get("X-ND-Authorization", "").startswith("Bearer jwt-")
        assert "Authorization" not in request.headers
        assert "jwt" not in request.query
        if state["reject_native"]:
            state["reject_native"] -= 1
            return web.Response(status=401)
        if request.path.endswith("/user"):
            start = int(request.query["_start"])
            end = int(request.query["_end"])
            return web.json_response(
                state["users"][start:end],
                headers={
                    "x-total-count": str(len(state["users"])),
                    "X-ND-Authorization": "jwt-renewed",
                },
            )
        if request.path.endswith("getCoverArt.view"):
            return web.Response(body=b"image-bytes", content_type="image/png")
        assert request.query["t"] == "salted-token"
        assert request.query["s"] == "salt"
        root = {
            "type": "navidrome",
            "status": "ok",
            "serverVersion": state["version"],
            "nowPlaying": {"entry": state["entries"]},
        }
        if state["rest_error"]:
            root.update(status="failed", error={"code": state["rest_error"]})
        return web.json_response({"subsonic-response": root})

    async def events(request):
        response = web.StreamResponse(headers={"Content-Type": state["stream_type"]})
        await response.prepare(request)
        queue = asyncio.Queue()
        state["streams"].add(queue)
        try:
            await response.write(
                (
                    "event: serverStart\ndata: "
                    + json.dumps({"version": state["version"]})
                    + "\n\n"
                ).encode()
            )
            while (frame := await queue.get()) is not None:
                await response.write(frame)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            state["streams"].discard(queue)
        return response

    app = web.Application()
    app.router.add_post("/music/auth/login", login)
    app.router.add_get("/music/api/user", authenticated)
    app.router.add_get("/music/api/events", events)
    app.router.add_get("/music/rest/{endpoint}", authenticated)
    http_server = await aiohttp_server(app, shutdown_timeout=0.1)
    state["url"] = str(http_server.make_url("/music"))
    yield state
    for queue in list(state["streams"]):
        await queue.put(None)


@pytest.fixture
async def client(server):
    from custom_components.navidrome.api import NavidromeClient

    async with ClientSession() as session:
        yield NavidromeClient(session, server["url"], "admin", "secret")


def entry(**overrides):
    """A reported snapshot entry with intentionally useless synthetic playerId."""
    return {
        "username": "Alice",
        "playerName": "Phone",
        "id": "track-1",
        "title": "First",
        "artist": "Artist",
        "album": "Album",
        "albumArtist": "Album Artist",
        "coverArt": "cover-1",
        "duration": 240,
        "positionMs": 10000,
        "playbackRate": 1,
        "state": "playing",
        "playerId": 1,
    } | overrides
