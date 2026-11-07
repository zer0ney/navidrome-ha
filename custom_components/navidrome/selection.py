"""Best-effort observed-change arbitration, independent of HA and wall-clock timers."""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime

from .api import ProtocolError
from .models import Session, User


@dataclass(frozen=True, slots=True)
class Playback:
    """Only the chosen session is published; its position timestamp stays anchored."""

    user: User
    selected: Session | None
    position_updated_at: datetime | None
    changed_at: datetime | None
    session_count: int


@dataclass(frozen=True, slots=True)
class Observation:
    """Internal heuristic match token, never a server/device identity."""

    token: int
    session: Session
    at: datetime
    revision: int


def expected_position(observation: Observation, now: datetime) -> float | None:
    """Extrapolate only reports with known playing state, position and rate."""
    session = observation.session
    if session.position is None:
        return None
    elapsed = max(0, (now - observation.at).total_seconds())
    position = session.position
    if session.state == "playing" and session.rate is not None:
        position += elapsed * session.rate
    return min(position, session.duration) if session.duration is not None else position


def meaningful(previous: Observation, current: Session, now: datetime) -> bool:
    """Normal clock progress and synthetic player IDs are not user changes."""
    if replace(previous.session, username="", position=None) != replace(
        current, username="", position=None
    ):
        return True
    expected = expected_position(previous, now)
    if expected is None or current.position is None:
        return expected != current.position
    return abs(current.position - expected) > 2


class SelectionReducer:
    """Track duplicate client labels without overwriting or inventing identities."""

    def __init__(self) -> None:
        self._observations: dict[str, list[Observation]] = {}
        self._chosen: dict[str, int] = {}
        self._views: dict[str, Playback] = {}
        self._serial = 0
        self._revision = 0

    def reduce(
        self,
        users: dict[str, User],
        sessions: list[Session],
        now: datetime,
        *,
        resync: bool = False,
    ) -> dict[str, Playback]:
        """Apply one ordered snapshot atomically, preserving selection on reconnect."""
        lookup = {user.username.casefold(): user.id for user in users.values()}
        if len(lookup) != len(users):
            raise ProtocolError("Ambiguous case-insensitive user roster")
        grouped: dict[str, list[Session]] = defaultdict(list)
        for session in sessions:
            if (user_id := lookup.get(session.username.casefold())) is not None:
                if session.state != "stopped":
                    grouped[user_id].append(replace(session, username=users[user_id].username))
        self._revision += 1
        result = {}
        next_observations = {}
        next_chosen = {}
        for user_id, user in users.items():
            old = self._observations.get(user_id, [])
            current = grouped[user_id]
            chosen = self._chosen.get(user_id)
            pairs = []
            for old_index, previous in enumerate(old):
                for new_index, session in enumerate(current):
                    if previous.session.client != session.client:
                        continue
                    expected = expected_position(previous, now)
                    drift = (
                        abs(session.position - expected)
                        if session.position is not None and expected is not None
                        else 0
                    )
                    cost = (
                        previous.session.track_id != session.track_id,
                        previous.session.state != session.state,
                        drift,
                        previous.token != chosen,
                        old_index,
                        new_index,
                    )
                    pairs.append((cost, old_index, new_index))
            matches = {}
            used = set()
            for _, old_index, new_index in sorted(pairs):
                if old_index not in used and new_index not in matches:
                    matches[new_index] = old[old_index]
                    used.add(old_index)
            observations = []
            changed = []
            for index, session in enumerate(current):
                previous = matches.get(index)
                if previous is None:
                    self._serial += 1
                    token = self._serial
                    revision = self._revision
                    changed.append(token)
                else:
                    token = previous.token
                    revision = previous.revision
                    if meaningful(previous, session, now):
                        revision = self._revision
                        changed.append(token)
                observations.append(Observation(token, session, now, revision))
            next_observations[user_id] = observations
            old_view = self._views.get(user_id)
            if not observations:
                changed_at = old_view.changed_at if old_view else None
                if old_view and old_view.selected:
                    changed_at = now
                result[user_id] = Playback(user, None, None, changed_at, 0)
                continue
            tokens = {observation.token for observation in observations}
            if chosen in tokens and (resync or not changed or chosen in changed):
                selected_token = chosen
            elif changed:
                selected_token = changed[0]
            else:
                selected_token = max(observations, key=lambda item: item.revision).token
            selection = next(item for item in observations if item.token == selected_token)
            next_chosen[user_id] = selected_token
            if old_view and chosen == selected_token and selected_token not in changed:
                result[user_id] = replace(old_view, user=user, session_count=len(observations))
            else:
                result[user_id] = Playback(user, selection.session, now, now, len(observations))
        self._observations = next_observations
        self._chosen = next_chosen
        self._views = result
        return result
