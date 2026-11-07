"""One nonblocking SSE reader and serialized snapshot worker per config entry."""

import asyncio
import logging
import random
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CannotConnect,
    InvalidAuth,
    NavidromeClient,
    NavidromeError,
    NotAdmin,
)
from .const import DOMAIN, ROSTER_INTERVAL, SNAPSHOT_INTERVAL, WATCHDOG
from .models import User
from .selection import Playback, SelectionReducer

_LOGGER = logging.getLogger(__name__)


def retry_delay(attempt: int) -> float:
    """Capped exponential retry with jitter, starting at approximately one second."""
    return min(60, 2 ** min(attempt, 6) * random.uniform(0.8, 1.2))


class NavidromeCoordinator(DataUpdateCoordinator[dict[str, Playback]]):
    """Publish current state only while both the stream and snapshots are healthy."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: NavidromeClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, always_update=False)
        self.api = api
        self.users: dict[str, User] = {}
        self.data = {}
        self._clock = clock
        self._reducer = SelectionReducer()
        self._tasks: set[asyncio.Task] = set()
        self._refresh_task: asyncio.Task | None = None
        self._stopped = False
        self._connected = False
        self._dirty = False
        self._roster_dirty = False
        self._resync = False
        self._epoch = 0
        self._last_roster = 0.0
        self._unsub_stop: Callable[[], None] | None = None
        self._ready: asyncio.Future[None] = hass.loop.create_future()
        self.last_update_success = False

    def _spawn(self, coroutine: Coroutine[Any, Any, None], name: str) -> asyncio.Task:
        task = self.hass.async_create_background_task(coroutine, f"navidrome {name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def async_start(self) -> None:
        """Setup is successful only after a subscribed, successful first snapshot."""
        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_hass_stop
        )
        try:
            await self.api.login()
            self._spawn(self._stream_loop(), "events")
            async with asyncio.timeout(WATCHDOG + 20):
                await self._ready
            self._spawn(self._periodic_loop(), "reconciliation")
        except BaseException:
            await self.async_stop()
            raise

    async def _async_hass_stop(self, event: Event) -> None:
        self._unsub_stop = None
        await self.async_stop()

    async def async_shutdown(self) -> None:
        """Include stream ownership in the coordinator's config-entry cleanup."""
        await self.async_stop()
        await super().async_shutdown()

    async def async_stop(self) -> None:
        """Cancel and join all owned work; never close HA's shared HTTP session."""
        self._stopped = True
        self._connected = False
        self._epoch += 1
        if self._unsub_stop:
            self._unsub_stop()
            self._unsub_stop = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not self._ready.done():
            self._ready.cancel()

    @callback
    def invalidate(self, *, roster: bool = False, resync: bool = False) -> None:
        """Every invalidation counts, even when nowPlayingCount has not changed."""
        if self._stopped:
            return
        self._dirty = True
        self._roster_dirty |= roster
        self._resync |= resync
        if self._connected and (self._refresh_task is None or self._refresh_task.done()):
            self._refresh_task = self._spawn(self._refresh_loop(), "snapshot")

    async def async_request_refresh(self) -> None:
        """Route HA's manual refresh requests through the same serialized worker."""
        self.invalidate()
        if self._refresh_task:
            await asyncio.shield(self._refresh_task)

    def _failed(self, error: NavidromeError) -> None:
        if isinstance(error, (InvalidAuth, NotAdmin)):
            failure = ConfigEntryAuthFailed("Navidrome administrator credentials rejected")
            self.async_set_update_error(failure)
            if self.config_entry:
                self.config_entry.async_start_reauth(self.hass)
        else:
            self.async_set_update_error(UpdateFailed("Navidrome stream or snapshot unavailable"))
        if not self._ready.done():
            self._ready.set_exception(error)

    async def _stream_loop(self) -> None:
        attempt = 0
        while not self._stopped:
            connected_at = time.monotonic()
            try:
                async with self.api.events() as events:
                    while not self._stopped:
                        async with asyncio.timeout(WATCHDOG):
                            event = await anext(events)
                        if event.event == "serverStart":
                            self.api.check_server_start(event)
                            self._epoch += 1
                            self._connected = True
                            self.invalidate(roster=True, resync=True)
                        elif event.event == "nowPlayingCount":
                            self.invalidate()
            except (NavidromeError, TimeoutError, StopAsyncIteration) as error:
                self._connected = False
                self._epoch += 1
                failure = error if isinstance(error, NavidromeError) else CannotConnect("SSE lost")
                initial = not self._ready.done()
                self._failed(failure)
                if isinstance(failure, (InvalidAuth, NotAdmin)) or initial:
                    return
                if time.monotonic() - connected_at >= WATCHDOG:
                    attempt = 0
                await asyncio.sleep(retry_delay(attempt))
                attempt += 1

    async def _refresh_loop(self) -> None:
        attempt = 0
        while self._dirty and self._connected and not self._stopped:
            self._dirty = False
            roster = self._roster_dirty or time.monotonic() - self._last_roster >= ROSTER_INTERVAL
            resync = self._resync
            self._roster_dirty = False
            self._resync = False
            epoch = self._epoch
            try:
                users = await self.api.users() if roster else self.users
                sessions = await self.api.now_playing()
                observed_at = self._clock()
                known_names = {user.username.casefold() for user in users.values()}
                if any(session.username.casefold() not in known_names for session in sessions):
                    users = await self.api.users()
                    roster = True
                if epoch != self._epoch or not self._connected or self._stopped:
                    continue
                data = self._reducer.reduce(users, sessions, observed_at, resync=resync)
                self.users = users
                if roster:
                    self._last_roster = time.monotonic()
                if not self.last_update_success or self.data != data:
                    self.async_set_updated_data(data)
                if not self._ready.done():
                    self._ready.set_result(None)
                attempt = 0
            except NavidromeError as error:
                self._failed(error)
                if isinstance(error, (InvalidAuth, NotAdmin)):
                    self._connected = False
                    return
                self._dirty = True
                self._roster_dirty |= roster
                self._resync |= resync
                await asyncio.sleep(retry_delay(attempt))
                attempt += 1

    async def _periodic_loop(self) -> None:
        while not self._stopped:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
            self.invalidate()
