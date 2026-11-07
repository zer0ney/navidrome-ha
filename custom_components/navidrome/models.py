"""Minimal, immutable server data retained by the integration."""

import math
from dataclasses import dataclass
from typing import Any


def text(value: Any) -> str | None:
    """Accept only nonempty strings from optional API fields."""
    return value if isinstance(value, str) and value else None


def number(value: Any) -> float | None:
    """Reject booleans, nonfinite numbers and negative positions/durations."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and value >= 0:
            return float(value)
    return None


@dataclass(frozen=True, slots=True)
class User:
    """Stable identity, not the admin API's private account details."""

    id: str
    username: str


@dataclass(frozen=True, slots=True)
class Session:
    """A snapshot entry; deliberately ignores the synthetic playerId."""

    username: str
    client: str
    track_id: str
    state: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    cover_id: str | None = None
    duration: float | None = None
    position: float | None = None
    rate: float | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Session:
        """Map only fields needed by a read-only media player."""
        username = text(data.get("username"))
        track_id = text(data.get("id"))
        if username is None or track_id is None:
            raise ValueError("Missing snapshot identity")
        position_ms = number(data.get("positionMs"))
        return cls(
            username=username,
            client=text(data.get("playerName")) or "",
            track_id=track_id,
            state=text(data.get("state")),
            title=text(data.get("title")),
            artist=text(data.get("artist")),
            album=text(data.get("album")),
            album_artist=text(data.get("albumArtist")),
            cover_id=text(data.get("coverArt")) or f"mf-{track_id}",
            duration=number(data.get("duration")),
            position=None if position_ms is None else position_ms / 1000,
            rate=number(data.get("playbackRate")),
        )
