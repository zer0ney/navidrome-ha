"""Observed-change arbitration and nonblocking push lifecycle tests."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.navidrome.api import CannotConnect
from custom_components.navidrome.coordinator import NavidromeCoordinator, retry_delay
from custom_components.navidrome.models import Session, User
from custom_components.navidrome.selection import SelectionReducer

from .conftest import entry

NOW = datetime(2026, 11, 7, tzinfo=UTC)
USERS = {"user-a": User("user-a", "Alice"), "user-b": User("user-b", "Bob")}


def session(**overrides):
    return Session.from_json(entry(**overrides))


def test_latest_changed_not_prefer_playing():
    reducer = SelectionReducer()
    phone = session()
    tablet = session(playerName="Tablet", id="other", state="paused")
    initial = reducer.reduce(USERS, [phone, tablet], NOW)
    assert initial["user-a"].selected.client == "Phone"
    assert initial["user-b"].selected is None
    changed = reducer.reduce(USERS, [phone, replace(tablet, position=60)], NOW)
    assert changed["user-a"].selected.client == "Tablet"
    assert changed["user-a"].selected.state == "paused"


def test_progress_not_a_change_or_state_write():
    reducer = SelectionReducer()
    phone, tablet = session(), session(playerName="Tablet", id="other")
    initial = reducer.reduce(USERS, [phone, tablet], NOW)
    later = reducer.reduce(
        USERS,
        [replace(phone, position=70), replace(tablet, position=70)],
        NOW + timedelta(seconds=60),
    )
    assert initial == later


def test_duplicate_labels_are_a_multiset_and_reorder_is_not_change():
    reducer = SelectionReducer()
    first, second = session(), session(id="other", positionMs=30000)
    initial = reducer.reduce(USERS, [first, second], NOW)
    reordered = reducer.reduce(USERS, [second, first], NOW)
    assert initial == reordered
    assert reordered["user-a"].session_count == 2
    assert (
        reducer.reduce(USERS, [first, replace(second, state="paused")], NOW)[
            "user-a"
        ].selected.track_id
        == "other"
    )


def test_identical_sessions_are_not_collapsed():
    view = SelectionReducer().reduce(USERS, [session(), session()], NOW)["user-a"]
    assert view.session_count == 2


def test_seek_pause_resume_ties_reconnect_and_disappearance():
    reducer = SelectionReducer()
    first, second = session(), session(playerName="Tablet", id="other")
    reducer.reduce(USERS, [first, second], NOW)
    changed = reducer.reduce(
        USERS, [replace(first, state="paused"), replace(second, position=90)], NOW
    )
    assert changed["user-a"].selected.client == "Phone"
    resumed = replace(first, state="playing", position=50)
    assert reducer.reduce(USERS, [resumed, second], NOW)["user-a"].selected.position == 50
    assert (
        reducer.reduce(USERS, [resumed, replace(second, position=80)], NOW, resync=True)[
            "user-a"
        ].selected.client
        == "Phone"
    )
    assert reducer.reduce(USERS, [second], NOW)["user-a"].selected.client == "Tablet"
    idle = reducer.reduce(USERS, [], NOW)["user-a"]
    assert idle.selected is None and idle.position_updated_at is None


def test_user_id_is_identity_case_insensitive_names():
    reducer = SelectionReducer()
    initial = reducer.reduce(USERS, [session(username="ALICE"), session(username="Bob")], NOW)
    renamed = USERS | {"user-a": User("user-a", "Renamed")}
    result = reducer.reduce(renamed, [session(username="Renamed"), session(username="Bob")], NOW)
    assert result["user-b"] == initial["user-b"]
    assert result["user-a"].user.username == "Renamed"
    recreated = {"new-id": User("new-id", "Renamed")}
    assert set(reducer.reduce(recreated, [], NOW)) == {"new-id"}


async def test_stream_initial_snapshot_and_unload(hass, client, server):
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    server["entries"] = [entry()]
    await coordinator.async_start()
    assert coordinator.last_update_success
    assert len(coordinator.data) == 2
    await coordinator.async_stop()
    assert not coordinator._tasks
    assert not client._session.closed


async def test_lyrics_are_nonblocking_and_deduplicated(hass, client, server):
    server["entries"] = [entry(), entry(username="Bob")]
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    entered, release = asyncio.Event(), asyncio.Event()
    lyrics = {"synced": True, "line": [{"start": 0, "value": "A new day"}]}

    async def blocked_lyrics(song_id):
        entered.set()
        await release.wait()
        return lyrics

    with patch.object(client, "lyrics", new=AsyncMock(side_effect=blocked_lyrics)) as fetch:
        await coordinator.async_start()
        async with asyncio.timeout(2):
            await entered.wait()
        assert coordinator.last_update_success
        assert coordinator.data["user-a"].selected.title == "First"
        assert coordinator.lyrics_for("track-1") == ("loading", None)
        coordinator.invalidate()
        await coordinator._refresh_task
        fetch.assert_awaited_once_with("track-1")
        release.set()
        await asyncio.gather(*coordinator._lyrics_tasks.values())
        assert coordinator.lyrics_for("track-1") == ("synced", lyrics)
    await coordinator.async_stop()
    assert not coordinator._lyrics_tasks


async def test_lyrics_failure_does_not_fail_playback(hass, client, server):
    server["entries"] = [entry()]
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    with patch.object(
        client, "lyrics", new=AsyncMock(side_effect=CannotConnect("failed"))
    ) as fetch:
        await coordinator.async_start()
        await asyncio.gather(*coordinator._lyrics_tasks.values())
        assert coordinator.last_update_success
        assert coordinator.lyrics_for("track-1") == ("error", None)
        coordinator.invalidate()
        await coordinator._refresh_task
        fetch.assert_awaited_once()
    await coordinator.async_stop()


async def test_missing_lyrics_are_cached(hass, client, server):
    server["entries"] = [entry()]
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    await coordinator.async_start()
    await asyncio.gather(*coordinator._lyrics_tasks.values())
    assert coordinator.lyrics_for("track-1") == ("none", None)
    coordinator.invalidate()
    await coordinator._refresh_task
    assert sum(path.endswith("/getLyricsBySongId") for path, _, _ in server["requests"]) == 1
    await coordinator.async_stop()


async def test_unload_cancels_pending_lyrics(hass, client, server):
    server["entries"] = [entry()]
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    entered = asyncio.Event()

    async def blocked_lyrics(song_id):
        entered.set()
        await asyncio.Event().wait()

    with patch.object(client, "lyrics", new=AsyncMock(side_effect=blocked_lyrics)):
        try:
            await coordinator.async_start()
            async with asyncio.timeout(2):
                await entered.wait()
            async with asyncio.timeout(2):
                await coordinator.async_stop()
            assert not coordinator._tasks
            assert not coordinator._lyrics_tasks
        finally:
            await coordinator.async_stop()


@pytest.mark.parametrize("cover", ["https://coverartarchive.org/release/example/front", None])
async def test_static_cover_is_nonblocking_and_cached(hass, client, server, cover):
    server["entries"] = [entry(albumId="album-1"), entry(username="Bob", albumId="album-1")]
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_cover(album_id):
        entered.set()
        await release.wait()
        return cover

    with patch.object(client, "static_cover", new=AsyncMock(side_effect=blocked_cover)) as fetch:
        try:
            await coordinator.async_start()
            async with asyncio.timeout(2):
                await entered.wait()
            assert coordinator.last_update_success
            assert coordinator.data["user-a"].selected.title == "First"
            coordinator.invalidate()
            await coordinator._refresh_task
            fetch.assert_awaited_once_with("album-1")
            pending = coordinator._static_cover_tasks["album-1"]
            release.set()
            await pending
            assert coordinator.static_cover_for("album-1") == cover
            server["entries"] = [entry(id="track-2", albumId="album-1")]
            coordinator.invalidate()
            await coordinator._refresh_task
            fetch.assert_awaited_once()
            coordinator._static_covers["album-1"] = (0, cover)
            coordinator.invalidate()
            await coordinator._refresh_task
            await asyncio.gather(*coordinator._static_cover_tasks.values())
            assert fetch.await_count == 2
        finally:
            release.set()
            await coordinator.async_stop()


async def test_static_cover_failure_does_not_fail_playback(hass, client, server):
    server["entries"] = [entry(albumId="album-1")]
    config_entry = Mock()
    coordinator = NavidromeCoordinator(hass, config_entry, client)
    failure = CannotConnect("Cover Art Archive returned HTTP 503")
    with patch.object(client, "static_cover", new=AsyncMock(side_effect=failure)) as fetch:
        try:
            await coordinator.async_start()
            await asyncio.gather(*coordinator._static_cover_tasks.values())
            assert coordinator.last_update_success
            assert coordinator.static_cover_for("album-1") is None
            config_entry.async_start_reauth.assert_not_called()
            coordinator.invalidate()
            await coordinator._refresh_task
            fetch.assert_awaited_once()
        finally:
            await coordinator.async_stop()


async def test_unload_cancels_pending_static_cover(hass, client, server):
    server["entries"] = [entry(albumId="album-1")]
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    entered = asyncio.Event()

    async def blocked_cover(album_id):
        entered.set()
        await asyncio.Event().wait()

    with patch.object(client, "static_cover", new=AsyncMock(side_effect=blocked_cover)):
        try:
            await coordinator.async_start()
            async with asyncio.timeout(2):
                await entered.wait()
            async with asyncio.timeout(2):
                await coordinator.async_stop()
            assert not coordinator._tasks
            assert not coordinator._static_cover_tasks
        finally:
            await coordinator.async_stop()


async def test_same_count_burst_has_immediate_trailing_refresh(hass, client, server):
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    await coordinator.async_start()
    entered, release = asyncio.Event(), asyncio.Event()
    original = client.now_playing
    calls = 0

    async def blocked_snapshot():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original()

    client.now_playing = blocked_snapshot
    coordinator.invalidate()
    await entered.wait()
    for queue in server["streams"]:
        await queue.put(b'event: nowPlayingCount\ndata: {"count":1}\n\n' * 20)
        await queue.put(b'event: keepAlive\ndata: {"ts":1}\n\n')
    server["entries"] = [entry(state="paused")]
    for _ in range(50):
        if coordinator._dirty:
            break
        await asyncio.sleep(0)
    assert coordinator._dirty
    release.set()
    await coordinator._refresh_task
    assert calls == 2
    assert coordinator.data["user-a"].selected.state == "paused"
    await coordinator.async_stop()


async def test_lost_stream_discards_inflight_snapshot(hass, client, server):
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    await coordinator.async_start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_snapshot():
        entered.set()
        await release.wait()
        return [session()]

    client.now_playing = blocked_snapshot
    coordinator.invalidate()
    await entered.wait()
    coordinator._connected = False
    coordinator._epoch += 1
    coordinator._failed(CannotConnect("lost"))
    release.set()
    await coordinator._refresh_task
    assert not coordinator.last_update_success
    assert coordinator.data["user-a"].selected is None
    await coordinator.async_stop()


async def test_unknown_user_immediately_refreshes_roster(hass, client, server):
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    await coordinator.async_start()
    server["users"].append({"id": "new-id", "userName": "New"})
    server["entries"] = [entry(username="New")]
    coordinator.invalidate()
    await coordinator._refresh_task
    assert coordinator.data["new-id"].selected.title == "First"
    await coordinator.async_stop()


def test_backoff_bounds():
    assert 0.8 <= retry_delay(0) <= 1.2
    assert 1.6 <= retry_delay(1) <= 2.4
    assert 51.2 <= retry_delay(20) <= 60


async def test_unchanged_snapshot_does_not_notify_listeners(hass, client, server):
    server["entries"] = [entry()]
    coordinator = NavidromeCoordinator(hass, Mock(), client, clock=lambda: NOW)
    await coordinator.async_start()
    await asyncio.gather(*coordinator._lyrics_tasks.values())
    listener = Mock()
    unsubscribe = coordinator.async_add_listener(listener)
    coordinator.invalidate()
    await coordinator._refresh_task
    listener.assert_not_called()
    unsubscribe()
    await coordinator.async_stop()


async def test_ha_stop_joins_owned_tasks(hass, client, server):
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    await coordinator.async_start()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert not coordinator._tasks
    assert coordinator._stopped
    assert not client._session.closed


async def test_initial_failed_snapshot_fails_setup_and_cleans_up(hass, client):
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    with patch.object(client, "now_playing", new=AsyncMock(side_effect=CannotConnect("failed"))):
        with pytest.raises(CannotConnect):
            await coordinator.async_start()
    assert not coordinator._tasks
    assert coordinator._unsub_stop is None


async def test_reconnect_resnapshots_and_refreshes_roster(hass, client, server):
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    await coordinator.async_start()
    disconnected = asyncio.Event()
    reconnected = asyncio.Event()

    def changed():
        if not coordinator.last_update_success:
            disconnected.set()
        elif disconnected.is_set():
            reconnected.set()

    unsubscribe = coordinator.async_add_listener(changed)
    server["entries"] = [entry(state="paused")]
    server["users"].append({"id": "new", "userName": "New"})
    with patch("custom_components.navidrome.coordinator.retry_delay", return_value=0):
        for queue in list(server["streams"]):
            await queue.put(None)
        async with asyncio.timeout(2):
            await reconnected.wait()
    assert disconnected.is_set()
    assert coordinator.data["user-a"].selected.state == "paused"
    assert coordinator.data["new"].selected is None
    unsubscribe()
    await coordinator.async_stop()


async def test_periodic_snapshot_recovers_dropped_event(hass, client, server):
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    await coordinator.async_start()
    server["entries"] = [entry(id="last-track")]
    coordinator._last_roster -= 301
    server["users"].append({"id": "idle-new", "userName": "Idle"})
    with patch(
        "custom_components.navidrome.coordinator.asyncio.sleep",
        new=AsyncMock(side_effect=[None, asyncio.CancelledError]),
    ) as timer:
        with pytest.raises(asyncio.CancelledError):
            await coordinator._periodic_loop()
        assert timer.call_args_list[0].args == (60,)
    await coordinator._refresh_task
    assert coordinator.data["user-a"].selected.track_id == "last-track"
    assert "idle-new" in coordinator.users
    await coordinator.async_stop()


async def test_watchdog_disconnects_and_resyncs(hass, client, server):
    coordinator = NavidromeCoordinator(hass, Mock(), client)
    await coordinator.async_start()
    real_timeout = asyncio.timeout
    armed = True
    unavailable, recovered = asyncio.Event(), asyncio.Event()

    def deadline(delay):
        nonlocal armed
        if armed and delay == 45:
            armed = False
            return real_timeout(0)
        return real_timeout(delay)

    def changed():
        if not coordinator.last_update_success:
            unavailable.set()
        elif unavailable.is_set():
            recovered.set()

    unsubscribe = coordinator.async_add_listener(changed)
    with (
        patch("custom_components.navidrome.coordinator.asyncio.timeout", side_effect=deadline),
        patch("custom_components.navidrome.coordinator.retry_delay", return_value=0),
    ):
        for queue in list(server["streams"]):
            await queue.put(b'event: keepAlive\ndata: {"ts":1}\n\n')
        async with real_timeout(2):
            await recovered.wait()
    assert not armed and unavailable.is_set()
    unsubscribe()
    await coordinator.async_stop()


async def test_runtime_auth_rejection_requests_reauth(hass, client, server):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    config_entry = Mock()
    coordinator = NavidromeCoordinator(hass, config_entry, client)
    await coordinator.async_start()
    server["rest_error"] = 40
    coordinator.invalidate()
    await coordinator._refresh_task
    assert not coordinator.last_update_success
    config_entry.async_start_reauth.assert_called_once_with(hass)
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    await coordinator.async_stop()
