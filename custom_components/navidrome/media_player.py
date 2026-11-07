"""Exactly one persistent, read-only now-playing entity per native user ID."""

from hashlib import sha256
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NavidromeConfigEntry
from .api import InvalidAuth, NavidromeError, NotAdmin
from .coordinator import NavidromeCoordinator
from .selection import Playback

STATES = {
    "starting": MediaPlayerState.BUFFERING,
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: NavidromeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add idle users too, and discover new accounts without reloading."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_users() -> None:
        new_ids = coordinator.users.keys() - known
        if new_ids:
            known.update(new_ids)
            async_add_entities(
                NavidromePlayer(coordinator, entry.entry_id, user_id) for user_id in sorted(new_ids)
            )

    add_users()
    entry.async_on_unload(coordinator.async_add_listener(add_users))


class NavidromePlayer(CoordinatorEntity[NavidromeCoordinator], MediaPlayerEntity):
    """Expose reported media, never speaker capabilities or playback controls."""

    _attr_should_poll = False
    _attr_supported_features = MediaPlayerEntityFeature(0)
    _attr_media_image_remotely_accessible = False
    _attr_icon = "mdi:music"

    def __init__(self, coordinator: NavidromeCoordinator, entry_id: str, user_id: str) -> None:
        super().__init__(coordinator, context=user_id)
        self._user_id = user_id
        self._username = coordinator.users[user_id].username
        self._attr_unique_id = f"{entry_id}:{user_id}"

    @property
    def playback(self) -> Playback | None:
        return self.coordinator.data.get(self._user_id)

    @property
    def available(self) -> bool:
        return super().available and self.playback is not None

    @property
    def name(self) -> str:
        if playback := self.playback:
            self._username = playback.user.username
        return f"Navidrome {self._username}"

    @property
    def state(self) -> MediaPlayerState | None:
        if not (playback := self.playback):
            return None
        if playback.selected is None:
            return MediaPlayerState.IDLE
        return STATES.get(playback.selected.state)

    def _media(self, field: str) -> Any:
        if (playback := self.playback) and playback.selected:
            return getattr(playback.selected, field)
        return None

    @property
    def media_content_type(self) -> MediaType | None:
        return MediaType.MUSIC if self._media("track_id") else None

    @property
    def media_content_id(self) -> str | None:
        return self._media("track_id")

    @property
    def media_title(self) -> str | None:
        return self._media("title")

    @property
    def media_artist(self) -> str | None:
        return self._media("artist")

    @property
    def media_album_name(self) -> str | None:
        return self._media("album")

    @property
    def media_album_artist(self) -> str | None:
        return self._media("album_artist")

    @property
    def media_duration(self) -> float | None:
        return self._media("duration")

    @property
    def media_position(self) -> float | None:
        if self._media("rate") != 1 or self.state not in STATES.values():
            return None
        return self._media("position")

    @property
    def media_position_updated_at(self):
        if self.media_position is not None and (playback := self.playback):
            return playback.position_updated_at
        return None

    @property
    def media_image_hash(self) -> str | None:
        if cover_id := self._media("cover_id"):
            return sha256(cover_id.encode()).hexdigest()[:24]
        return None

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Only HA contacts Navidrome; no credential-bearing browser URLs."""
        if self.available and (cover_id := self._media("cover_id")):
            try:
                return await self.coordinator.api.image(cover_id)
            except (InvalidAuth, NotAdmin) as error:
                self.coordinator._failed(error)
            except NavidromeError:
                pass
        return None, None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not (playback := self.playback):
            return {}
        return {
            "username": playback.user.username,
            "source_client": self._media("client"),
            "active_session_count": playback.session_count,
            "playback_rate": self._media("rate"),
            "last_observed_change": playback.changed_at.isoformat()
            if playback.changed_at
            else None,
        }
