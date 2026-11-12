"""Read-only entity metadata, image proxying, user identity and lifecycle."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from homeassistant.components.http import KEY_AUTHENTICATED
from homeassistant.components.media_player import MediaPlayerImageView, MediaPlayerState
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from custom_components.navidrome import async_setup, async_setup_entry, async_unload_entry
from custom_components.navidrome.api import CannotConnect, InvalidAuth
from custom_components.navidrome.coordinator import NavidromeCoordinator
from custom_components.navidrome.media_player import NavidromePlayer
from custom_components.navidrome.media_player import async_setup_entry as setup_players
from custom_components.navidrome.models import Session, User

from .conftest import entry

NOW = datetime(2026, 11, 7, tzinfo=UTC)


@pytest.fixture
def coordinator(hass, client):
    instance = NavidromeCoordinator(hass, Mock(), client, clock=lambda: NOW)
    instance.users = {"user-a": User("user-a", "Alice")}
    return instance


def publish(coordinator, entries):
    coordinator.async_set_updated_data(
        coordinator._reducer.reduce(
            coordinator.users, [Session.from_json(item) for item in entries], NOW
        )
    )


async def test_card_resource_registration(hass):
    from pathlib import Path

    hass.http = Mock(async_register_static_paths=AsyncMock())
    assert await async_setup(hass, {})
    paths = hass.http.async_register_static_paths.call_args.args[0]
    assert len(paths) == 1
    assert paths[0].url_path == "/navidrome/lyrics-card.js"
    assert Path(paths[0].path).is_file()


async def test_metadata_artwork_no_secrets(coordinator, hass, server):
    publish(coordinator, [entry()])
    player = NavidromePlayer(coordinator, "stable", "user-a")
    player.hass = hass
    player.entity_id = "media_player.navidrome_alice"
    assert player.unique_id == "stable:user-a"
    assert player.state == MediaPlayerState.PLAYING
    assert player.media_title == "First"
    assert player.media_album_artist == "Album Artist"
    assert player.media_duration == 240
    assert player.media_position == 10
    assert player.media_position_updated_at == NOW
    assert player.supported_features == 0
    assert not player.should_poll
    assert player.entity_picture.startswith("/api/media_player_proxy/")
    assert "music" not in player.entity_picture
    assert "secret" not in str(player.extra_state_attributes)
    assert await player.async_get_media_image() == (b"image-bytes", "image/png")
    assert server["requests"][-1][1]["id"] == "cover-1"


@pytest.mark.parametrize("cover_art", [None, ""])
async def test_artwork_without_cover_id(coordinator, hass, server, cover_art):
    report = entry(coverArt=cover_art)
    if cover_art is None:
        report.pop("coverArt")
    publish(coordinator, [report])
    player = NavidromePlayer(coordinator, "stable", "user-a")
    player.hass = hass
    player.entity_id = "media_player.navidrome_alice"
    assert player.entity_picture is not None
    request = make_mocked_request("GET", player.entity_picture)
    request[KEY_AUTHENTICATED] = False
    view = MediaPlayerImageView(Mock(get_entity=Mock(return_value=player)))
    response = await view.get(request, player.entity_id)
    assert response.status == 200
    assert response.body == b"image-bytes"
    assert response.content_type == "image/png"
    assert server["requests"][-1][1]["id"] == "mf-track-1"


async def test_static_cover_attribute_preserves_player_artwork(
    coordinator, hass, server, cover_archive
):
    release_id = "76df3287-6cda-33eb-8e9a-044b5e15ffdd"
    server["album"] = {"id": "album-1", "musicBrainzId": release_id}
    publish(coordinator, [entry(albumId="album-1")])
    player = NavidromePlayer(coordinator, "stable", "user-a")
    player.hass = hass
    player.entity_id = "media_player.navidrome_alice"
    original_artwork = player.entity_picture
    assert player.extra_state_attributes["cover_art_static"] is None
    try:
        coordinator._schedule_static_covers()
        await coordinator._static_cover_tasks["album-1"]
        assert player.extra_state_attributes["cover_art_static"] == (
            f"{cover_archive['url']}/release/{release_id}/front"
        )
        assert player.entity_picture == original_artwork
        assert await player.async_get_media_image() == (b"image-bytes", "image/png")
        assert server["requests"][-1][0] == "/music/rest/getCoverArt"
        publish(coordinator, [entry(id="track-2", albumId="album-2")])
        assert player.extra_state_attributes["cover_art_static"] is None
        publish(coordinator, [])
        assert player.extra_state_attributes["cover_art_static"] is None
    finally:
        await coordinator.async_stop()


async def test_late_static_cover_does_not_replace_current_album(coordinator):
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(album_id):
        if album_id == "album-1":
            entered.set()
            await release.wait()
        return f"https://example.com/{album_id}.jpg"

    player = NavidromePlayer(coordinator, "stable", "user-a")
    with patch.object(coordinator.api, "static_cover", new=AsyncMock(side_effect=resolve)):
        try:
            publish(coordinator, [entry(albumId="album-1")])
            coordinator._schedule_static_covers()
            original = coordinator._static_cover_tasks["album-1"]
            async with asyncio.timeout(2):
                await entered.wait()
            publish(coordinator, [entry(id="track-2", albumId="album-2")])
            coordinator._schedule_static_covers()
            await coordinator._static_cover_tasks["album-2"]
            release.set()
            await original
            assert player.extra_state_attributes["cover_art_static"] == (
                "https://example.com/album-2.jpg"
            )
        finally:
            release.set()
            await coordinator.async_stop()


async def test_late_lyrics_do_not_replace_current_track(coordinator):
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(song_id):
        if song_id == "track-1":
            entered.set()
            await release.wait()
        return {"synced": True, "line": [{"start": 0, "value": song_id}]}

    player = NavidromePlayer(coordinator, "stable", "user-a")
    with patch.object(coordinator.api, "lyrics", new=AsyncMock(side_effect=resolve)):
        try:
            publish(coordinator, [entry()])
            coordinator._schedule_lyrics()
            original = coordinator._lyrics_tasks["track-1"]
            async with asyncio.timeout(2):
                await entered.wait()
            assert player.extra_state_attributes["lyrics_status"] == "loading"
            publish(coordinator, [entry(id="track-2")])
            coordinator._schedule_lyrics()
            await coordinator._lyrics_tasks["track-2"]
            release.set()
            await original
            attributes = player.extra_state_attributes
            assert attributes["lyrics_status"] == "synced"
            assert attributes["lyrics"]["song_id"] == "track-2"
            assert attributes["lyrics"]["line"][0]["value"] == "track-2"
            publish(coordinator, [])
            assert player.extra_state_attributes["lyrics_status"] == "none"
            assert player.extra_state_attributes["lyrics"] is None
        finally:
            release.set()
            await coordinator.async_stop()


async def test_artwork_failure_is_logged(coordinator, hass, server, caplog):
    publish(coordinator, [entry()])
    player = NavidromePlayer(coordinator, "stable", "user-a")
    player.hass = hass
    player.entity_id = "media_player.navidrome_alice"
    server["artwork_response"] = {
        "body": b"<html>secret</html>",
        "content_type": "text/html",
    }
    request = make_mocked_request("GET", player.entity_picture)
    request[KEY_AUTHENTICATED] = False
    view = MediaPlayerImageView(Mock(get_entity=Mock(return_value=player)))
    response = await view.get(request, player.entity_id)
    assert response.status == 404
    assert "Unable to fetch Navidrome artwork" in caplog.text
    assert "text/html" in caplog.text
    assert "secret" not in caplog.text
    assert "salted-token" not in caplog.text


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("starting", MediaPlayerState.BUFFERING),
        ("paused", MediaPlayerState.PAUSED),
        (None, None),
        ("unrecognized", None),
    ],
)
async def test_reported_states(coordinator, state, expected):
    publish(coordinator, [entry(state=state)])
    assert NavidromePlayer(coordinator, "stable", "user-a").state == expected


async def test_missing_position_and_nonunit_rate(coordinator):
    player = NavidromePlayer(coordinator, "stable", "user-a")
    for report in [entry(positionMs=None), entry(playbackRate=1.5)]:
        publish(coordinator, [report])
        assert player.media_position is None
        assert player.media_position_updated_at is None


async def test_idle_unavailable_rename_delete(coordinator):
    publish(coordinator, [entry()])
    player = NavidromePlayer(coordinator, "stable", "user-a")
    publish(coordinator, [])
    assert player.state == MediaPlayerState.IDLE
    assert player.available
    assert player.media_title is None and player.media_image_hash is None
    coordinator.users["user-a"] = User("user-a", "Renamed")
    publish(coordinator, [])
    assert player.name == "Navidrome Renamed"
    assert player.unique_id == "stable:user-a"
    coordinator.users = {}
    publish(coordinator, [])
    assert not player.available


async def test_dynamic_users_not_clients(coordinator, hass):
    added = []
    callbacks = []
    config_entry = Mock(runtime_data=coordinator, entry_id="stable")
    config_entry.async_on_unload.side_effect = callbacks.append
    await setup_players(hass, config_entry, lambda entities: added.extend(entities))
    assert len(added) == 1
    publish(coordinator, [entry(), entry(playerName="Tablet")])
    assert len(added) == 1
    coordinator.users["user-b"] = User("user-b", "Bob")
    publish(coordinator, [])
    assert len(added) == 2
    for unsubscribe in callbacks:
        unsubscribe()
    assert not coordinator._listeners


async def test_setup_unload(coordinator, hass):
    config_entry = Mock(
        data={"url": "https://music.example", "username": "admin", "password": "secret"}
    )
    hass.config_entries = Mock(
        async_forward_entry_setups=AsyncMock(), async_unload_platforms=AsyncMock(return_value=True)
    )
    with (
        patch("custom_components.navidrome.NavidromeCoordinator", return_value=coordinator),
        patch.object(coordinator, "async_start", new=AsyncMock()),
        patch.object(coordinator, "async_stop", new=AsyncMock()) as stop,
    ):
        assert await async_setup_entry(hass, config_entry) is None
        assert config_entry.runtime_data is coordinator
        assert await async_unload_entry(hass, config_entry)
        stop.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "expected"),
    [(InvalidAuth, ConfigEntryAuthFailed), (CannotConnect, ConfigEntryNotReady)],
)
async def test_setup_errors(coordinator, hass, error, expected):
    config_entry = Mock(
        data={"url": "https://music.example", "username": "admin", "password": "secret"}
    )
    with (
        patch("custom_components.navidrome.NavidromeCoordinator", return_value=coordinator),
        patch.object(coordinator, "async_start", new=AsyncMock(side_effect=error("failed"))),
    ):
        with pytest.raises(expected):
            await async_setup_entry(hass, config_entry)


async def test_real_ha_entry_and_registry_lifecycle(hass, server):
    """Load through HA itself, not a mocked forwarding or entity registry."""
    from types import MappingProxyType

    from homeassistant import auth, bootstrap, config_entries, loader
    from homeassistant.helpers import entity_registry as er

    loader.async_setup(hass)
    hass.config.skip_pip = True
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    assert await bootstrap.async_load_base_functionality(hass)
    hass.auth = await auth.auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
    config_entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain="navidrome",
        title="Navidrome",
        data={"url": server["url"], "username": "admin", "password": "secret"},
        options={},
        source="user",
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    await hass.config_entries.async_add(config_entry)
    await hass.async_block_till_done()
    assert config_entry.state == config_entries.ConfigEntryState.LOADED
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    assert len(entities) == 2
    assert {entity.unique_id for entity in entities} == {
        f"{config_entry.entry_id}:user-a",
        f"{config_entry.entry_id}:user-b",
    }
    assert all(hass.states.get(entity.entity_id).state == "idle" for entity in entities)
    alice = next(entity for entity in entities if entity.unique_id.endswith(":user-a"))
    updated = asyncio.Event()

    def changed(event):
        state = event.data.get("new_state")
        if event.data["entity_id"] == alice.entity_id and state and state.state == "paused":
            updated.set()

    unsubscribe = hass.bus.async_listen("state_changed", changed)
    server["entries"] = [entry(state="paused")]
    for queue in list(server["streams"]):
        await queue.put(b'event: nowPlayingCount\ndata: {"count": 1}\n\n')
    async with asyncio.timeout(2):
        await updated.wait()
    unsubscribe()
    assert hass.states.get(alice.entity_id).attributes["media_title"] == "First"
    runtime = config_entry.runtime_data
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert not runtime._tasks
    assert {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    } == {entity.entity_id for entity in entities}
    runtime = config_entry.runtime_data
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert not runtime._tasks
    assert not runtime._listeners
