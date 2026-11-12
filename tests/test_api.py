"""Source-shaped API/SSE contracts exercised over real aiohttp sockets."""

import json

import pytest
from httpx_sse._models import ServerSentEvent

from custom_components.navidrome.api import (
    CannotConnect,
    InvalidAuth,
    NotAdmin,
    NotSupported,
    ProtocolError,
    normalize_url,
)
from custom_components.navidrome.const import MAX_EVENT_CHARS

from .conftest import entry


async def test_read_only_native_push_contract(client, server):
    await client.validate()
    assert client.version == "0.64.2 (source-fixture)"
    users = await client.users()
    assert set(users) == {"user-a", "user-b"}
    async with client.events() as events:
        assert (await anext(events)).event == "serverStart"
        for state in ("playing", "paused", "playing"):
            server["entries"] = [entry(state=state)]
            for queue in server["streams"]:
                await queue.put(b'event: nowPlayingCount\ndata: {"count":1}\n\n')
            assert (await anext(events)).event == "nowPlayingCount"
            snapshot = await client.now_playing()
            assert snapshot[0].state == state
        server["entries"] = [entry(id="track-2"), entry(username="Bob")]
        assert [session.track_id for session in await client.now_playing()] == [
            "track-2",
            "track-1",
        ]
        server["entries"] = []
        assert await client.now_playing() == []
    assert server["login_count"] == 1
    assert all(path.startswith("/music/") for path, _, _ in server["requests"])
    assert server["requests"][-1][2]["X-ND-Authorization"] == "Bearer jwt-renewed"


async def test_roster_pagination(client, server):
    server["users"] = [{"id": str(index), "userName": f"user{index}"} for index in range(205)]
    assert len(await client.users()) == 205
    assert [query["_start"] for _, query, _ in server["requests"]] == ["0", "100", "200"]


async def test_split_utf8_multiline_crlf_unknown(client, server):
    async with client.events() as events:
        await anext(events)
        frame = ('event: strange\r\ndata: {"title":\r\ndata: "caf\u00e9"}\r\n\r\n').encode()
        for queue in server["streams"]:
            for byte in frame:
                await queue.put(bytes([byte]))
        event = await anext(events)
        assert event.event == "strange"
        assert json.loads(event.data) == {"title": "caf\u00e9"}


@pytest.mark.parametrize("frame", [b"data: " + b"x" * (MAX_EVENT_CHARS + 1), b"data: x\n" * 10000])
async def test_bounded_sse(client, server, frame):
    async with client.events() as events:
        await anext(events)
        for queue in server["streams"]:
            await queue.put(frame)
        with pytest.raises(ProtocolError):
            await anext(events)


async def test_html_is_not_sse(client, server):
    server["stream_type"] = "text/html"
    with pytest.raises(NotSupported):
        await client.validate()


@pytest.mark.parametrize(
    ("admin", "status", "error"), [(False, 200, NotAdmin), (True, 401, InvalidAuth)]
)
async def test_admin_required(client, server, admin, status, error):
    server.update(admin=admin, status=status)
    with pytest.raises(error):
        await client.login()


@pytest.mark.parametrize(("code", "error"), [(40, InvalidAuth), (70, ProtocolError)])
async def test_subsonic_failure_200(client, server, code, error):
    server["rest_error"] = code
    with pytest.raises(error):
        await client.now_playing()


@pytest.mark.parametrize("outcome", ["synced", "unsynced", "none"])
async def test_lyrics_outcomes(client, server, outcome):
    lines = [{"value": "A new day"}, {"value": "Another sky"}]
    if outcome == "synced":
        lines = [dict(line, start=index * 3500) for index, line in enumerate(lines)]
    if outcome != "none":
        server["lyrics"] = [{"synced": outcome == "synced", "lang": "eng", "line": lines}]
    lyrics = await client.lyrics("track-1")
    if outcome == "none":
        assert lyrics is None
    else:
        assert lyrics["synced"] is (outcome == "synced")
        assert lyrics["line"] == lines
        assert lyrics["lang"] == "eng"
    path, query, _ = server["requests"][-1]
    assert path == "/music/rest/getLyricsBySongId"
    assert query["id"] == "track-1"


async def test_lyrics_prefer_synced_and_sort_times(client, server):
    server["lyrics"] = [
        {"synced": False, "line": [{"value": "Plain lyrics"}]},
        {
            "synced": True,
            "offset": -100,
            "line": [{"start": 3000, "value": "Later"}, {"start": 1000, "value": "Earlier"}],
        },
    ]
    lyrics = await client.lyrics("track-1")
    assert lyrics["synced"] is True
    assert lyrics["offset"] == -100
    assert [line["start"] for line in lyrics["line"]] == [1000, 3000]


async def test_lyrics_missing_timestamps_are_unsynced(client, server):
    server["lyrics"] = [{"synced": True, "line": [{"value": "Keep these words"}]}]
    lyrics = await client.lyrics("track-1")
    assert lyrics["synced"] is False
    assert lyrics["line"] == [{"value": "Keep these words"}]


@pytest.mark.parametrize(
    ("code", "error"),
    [(40, InvalidAuth), (70, None), (10, ProtocolError), ({"invalid": True}, ProtocolError)],
)
async def test_lyrics_errors_are_not_absence(client, server, code, error):
    server["rest_error"] = code
    if error is None:
        assert await client.lyrics("track-1") is None
    else:
        with pytest.raises(error):
            await client.lyrics("track-1")


async def test_lyrics_attribute_size_is_bounded(client, server, monkeypatch):
    monkeypatch.setattr("custom_components.navidrome.api.MAX_LYRICS_BYTES", 100)
    server["lyrics"] = [{"synced": False, "line": [{"value": "x" * 200}]}]
    with pytest.raises(ProtocolError, match="attribute size limit"):
        await client.lyrics("track-1")


async def test_static_cover_uses_release_not_recording_id(client, server, cover_archive):
    release_id = "76df3287-6cda-33eb-8e9a-044b5e15ffdd"
    server["entries"] = [
        entry(albumId="album-1", musicBrainzId="99b09d02-9cc9-3fed-8431-f162165a9371")
    ]
    server["album"] = {"id": "album-1", "musicBrainzId": release_id}
    snapshot = await client.now_playing()
    assert snapshot[0].album_id == "album-1"
    assert await client.static_cover(snapshot[0].album_id) == (
        f"{cover_archive['url']}/release/{release_id}/front"
    )
    path, query, _ = server["requests"][-1]
    assert path == "/music/rest/getAlbum"
    assert query["id"] == "album-1"
    assert cover_archive["requests"] == [("HEAD", f"/release/{release_id}/front", {})]


@pytest.mark.parametrize("release_id", [None, "", "not-a-uuid", "../../private", 123])
async def test_static_cover_without_valid_release_id(client, server, cover_archive, release_id):
    server["album"] = {"id": "album-1", "musicBrainzId": release_id}
    assert await client.static_cover("album-1") is None
    assert not cover_archive["requests"]


async def test_static_cover_not_found(client, server, cover_archive):
    server["album"] = {"musicBrainzId": "76df3287-6cda-33eb-8e9a-044b5e15ffdd"}
    cover_archive["status"] = 404
    assert await client.static_cover("album-1") is None


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_static_cover_service_errors_are_not_navidrome_auth(
    client, server, cover_archive, status
):
    server["album"] = {"musicBrainzId": "76df3287-6cda-33eb-8e9a-044b5e15ffdd"}
    cover_archive["status"] = status
    with pytest.raises(CannotConnect, match=f"Cover Art Archive returned HTTP {status}"):
        await client.static_cover("album-1")


async def test_static_cover_accepts_image_response(client, server, cover_archive):
    release_id = "76df3287-6cda-33eb-8e9a-044b5e15ffdd"
    server["album"] = {"musicBrainzId": release_id}
    cover_archive["status"] = 200
    assert await client.static_cover("album-1") == (
        f"{cover_archive['url']}/release/{release_id}/front"
    )


@pytest.mark.parametrize(("code", "error"), [(40, InvalidAuth), (41, InvalidAuth), (70, None)])
async def test_static_cover_album_lookup_errors(client, server, cover_archive, code, error):
    server["rest_error"] = code
    if error is None:
        assert await client.static_cover("album-1") is None
    else:
        with pytest.raises(error):
            await client.static_cover("album-1")
    assert not cover_archive["requests"]


async def test_artwork_cache(client, server):
    assert await client.image("cover") == (b"image-bytes", "image/png")
    assert await client.image("cover") == (b"image-bytes", "image/png")
    assert len(server["requests"]) == 1
    assert server["requests"][0][0] == "/music/rest/getCoverArt"


async def test_large_webp_artwork(client, server):
    body = b"webp-bytes" * (3 * 1024 * 1024 // 10)
    server["artwork_response"] = {"body": body, "content_type": "image/webp"}
    assert await client.image("animated-cover") == (body, "image/webp")
    assert await client.image("animated-cover") == (body, "image/webp")
    assert len(server["requests"]) == 1


async def test_artwork_cache_byte_budget(client, server, monkeypatch):
    monkeypatch.setattr(
        "custom_components.navidrome.api.MAX_IMAGE_CACHE_BYTES", 2 * len(b"image-bytes")
    )
    for cover_id in ["cover-1", "cover-2", "cover-1", "cover-3", "cover-1", "cover-2"]:
        assert await client.image(cover_id) == (b"image-bytes", "image/png")
    assert [query["id"] for _, query, _ in server["requests"]] == [
        "cover-1",
        "cover-2",
        "cover-3",
        "cover-2",
    ]


@pytest.mark.parametrize("size", [64, 65])
async def test_artwork_download_limit(client, server, monkeypatch, size):
    monkeypatch.setattr("custom_components.navidrome.api.MAX_IMAGE_BYTES", 64)
    body = b"x" * size
    server["artwork_response"] = {"body": body, "content_type": "image/webp"}
    if size == 64:
        assert await client.image("cover") == (body, "image/webp")
    else:
        with pytest.raises(ProtocolError, match="size limit"):
            await client.image("cover")
        assert not client._images


@pytest.mark.parametrize(
    ("status", "content_type", "code", "error", "message"),
    [
        (404, "text/plain", None, NotSupported, "HTTP 404"),
        (200, "text/html", None, ProtocolError, "text/html"),
        (200, "application/json", 40, InvalidAuth, "Subsonic code 40"),
        (200, "application/json", 70, ProtocolError, "Subsonic code 70"),
    ],
)
async def test_artwork_errors_are_identifiable(
    client, server, status, content_type, code, error, message
):
    body = json.dumps(
        {"subsonic-response": {"status": "failed", "error": {"code": code, "message": "secret"}}}
    ).encode()
    server["artwork_response"] = {
        "body": body,
        "content_type": content_type,
        "status": status,
    }
    with pytest.raises(error, match=message) as caught:
        await client.image("cover")
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    ["ftp://host", "http://user:pass@host", "https://host?jwt=x", "http://host/#secret", "host"],
)
def test_unsafe_base_urls(url):
    with pytest.raises(ValueError):
        normalize_url(url)


def test_url_and_version(client):
    assert normalize_url(" HTTPS://Example.com:443/music/ ") == "https://example.com/music"
    with pytest.raises(NotSupported):
        client.check_server_start(ServerSentEvent(event="serverStart", data='{"version":"0.63.0"}'))


@pytest.mark.parametrize("entries", [{}, "bad", [42], [{"username": "Alice"}]])
async def test_malformed_entries_are_not_idle(client, server, entries):
    server["entries"] = entries
    with pytest.raises(ProtocolError):
        await client.now_playing()


async def test_duplicate_roster_id_rejected(client, server):
    server["users"].append({"id": "user-a", "userName": "Different"})
    with pytest.raises(ProtocolError):
        await client.users()


async def test_native_request_retries_once_after_token_rejection(client, server):
    await client.login()
    server["reject_native"] = 1
    assert len(await client.users()) == 2
    assert server["login_count"] == 2
