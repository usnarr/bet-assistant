"""As-of query helpers (F07.1, F07.4, F07.5). Availability is enforced here, once.

Every read returns the fact version known at the cutoff together with an
:class:`InputRef` that records how its availability was proven. Callers never see a fact
that the chosen mode cannot prove was available.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Protocol
from uuid import UUID

from tennis_engine.common.clock import require_aware
from tennis_engine.contracts.domain import Availability, AvailabilityClass
from tennis_engine.normalization.contracts import (
    Match,
    PlayerAlias,
    RankingSnapshot,
    ResultVersion,
    ScheduleVersion,
    StatsVersion,
    StatusVersion,
)
from tennis_engine.normalization.store import IdentityStore

from .contracts import AvailabilityMode, InputRef


class HasAvailability(Protocol):
    @property
    def availability(self) -> Availability: ...


def proof(
    availability: Availability, as_of: datetime, mode: AvailabilityMode
) -> AvailabilityClass | None:
    """Return how ``availability`` is proven at ``as_of`` under ``mode``, or ``None``."""
    proven = availability.available_by(as_of, allow_archived=mode != AvailabilityMode.PROSPECTIVE)
    if proven is not None:
        return proven
    if (
        mode == AvailabilityMode.RESEARCH_ONLY
        and availability.effective_at is not None
        and availability.effective_at <= as_of
    ):
        return AvailabilityClass.RESEARCH_ONLY
    return None


def latest_available[T: HasAvailability](
    versions: Sequence[T], as_of: datetime, mode: AvailabilityMode
) -> tuple[T, AvailabilityClass] | None:
    """Latest version that the mode proves available. Later versions are invisible."""
    for item in reversed(versions):
        proven = proof(item.availability, as_of, mode)
        if proven is not None:
            return item, proven
    return None


@dataclass(frozen=True)
class CompletedMatch:
    match: Match
    result: ResultVersion
    ended_at: datetime
    input: InputRef


class AsOfView:
    """Read-only view of the identity warehouse at one cutoff."""

    def __init__(self, store: IdentityStore, as_of: datetime, mode: AvailabilityMode) -> None:
        self.store = store
        self.as_of = require_aware(as_of)
        self.mode = mode
        self._completed: dict[UUID, list[CompletedMatch]] = {}

    def _ref(
        self,
        kind: str,
        key: str,
        version: int,
        item: HasAvailability,
        proven: AvailabilityClass,
        source_id: str,
    ) -> InputRef:
        return InputRef(
            kind=kind,
            key=key,
            version=version,
            availability=proven,
            observed_at=item.availability.observed_at,
            source_id=source_id,
        )

    def player_alias(self, source_id: str, source_player_id: str) -> PlayerAlias | None:
        """Alias versions are known from their recording time; later remaps are invisible."""
        return self.store.player_alias(source_id, source_player_id, as_of=self.as_of)

    def schedule(self, match_id: UUID) -> tuple[ScheduleVersion, InputRef] | None:
        found = latest_available(self.store.schedules(match_id), self.as_of, self.mode)
        if found is None:
            return None
        item, proven = found
        return item, self._ref(
            "schedule", str(match_id), item.version, item, proven, item.source_id
        )

    def status(self, match_id: UUID) -> tuple[StatusVersion, InputRef] | None:
        found = latest_available(self.store.statuses(match_id), self.as_of, self.mode)
        if found is None:
            return None
        item, proven = found
        return item, self._ref("status", str(match_id), item.version, item, proven, item.source_id)

    def result(self, match_id: UUID) -> tuple[ResultVersion, InputRef] | None:
        found = latest_available(self.store.results(match_id), self.as_of, self.mode)
        if found is None:
            return None
        item, proven = found
        return item, self._ref("result", str(match_id), item.version, item, proven, item.source_id)

    def stats(self, match_id: UUID, player_id: UUID) -> tuple[StatsVersion, InputRef] | None:
        found = latest_available(self.store.stats(match_id, player_id), self.as_of, self.mode)
        if found is None:
            return None
        item, proven = found
        key = f"{match_id}:{player_id}"
        return item, self._ref("stats", key, item.version, item, proven, item.source_id)

    def ranking(self, player_id: UUID) -> tuple[RankingSnapshot, InputRef] | None:
        """Latest ranking dated on or before the cutoff and proven available by it.

        A ranking published after the cutoff but dated earlier is rejected, because its
        observation, not its ranking date, decides availability.
        """
        best: tuple[RankingSnapshot, AvailabilityClass] | None = None
        for item in self.store.rankings(player_id):
            dated = datetime.combine(item.ranking_date, time(0), tzinfo=UTC)
            if dated > self.as_of:
                continue
            proven = proof(item.availability, self.as_of, self.mode)
            if proven is None:
                continue
            if best is None or (item.ranking_date, item.availability.observed_at) > (
                best[0].ranking_date,
                best[0].availability.observed_at,
            ):
                best = (item, proven)
        if best is None:
            return None
        item, proven = best
        key = f"{player_id}:{item.ranking_date.isoformat()}"
        return item, self._ref("ranking", key, 1, item, proven, item.source_id)

    def completed_matches(
        self, player_id: UUID, *, exclude: UUID | None = None
    ) -> list[CompletedMatch]:
        """Matches of ``player_id`` completed before the cutoff with a known result.

        A result must be proven available by the cutoff, and the match must have ended
        strictly before the cutoff. The target match is always excluded. Ties sort by end
        time, then match ID, so simultaneous events have one fixed order.
        """
        if player_id not in self._completed:
            found = []
            for match in self.store.matches():
                if player_id not in match.player_ids:
                    continue
                known = self.result(match.match_id)
                if known is None:
                    continue
                result, ref = known
                ended = result.availability.effective_at
                if ended is None or ended >= self.as_of:
                    continue
                found.append(CompletedMatch(match, result, ended, ref))
            found.sort(key=lambda item: (item.ended_at, str(item.match.match_id)))
            self._completed[player_id] = found
        return [item for item in self._completed[player_id] if item.match.match_id != exclude]

    def all_completed(self, *, exclude: UUID | None = None) -> list[CompletedMatch]:
        """Every completed match before the cutoff in the fixed chronological order."""
        seen: dict[UUID, CompletedMatch] = {}
        for player in self.store.players():
            for item in self.completed_matches(player.player_id, exclude=exclude):
                seen[item.match.match_id] = item
        return sorted(seen.values(), key=lambda item: (item.ended_at, str(item.match.match_id)))


def forecast_usable(
    *,
    issued_at: datetime,
    valid_from: datetime,
    valid_to: datetime,
    scheduled_start: datetime,
    as_of: datetime,
    realized: bool,
) -> bool:
    """A weather input is usable only if it is a forecast issued by the cutoff and valid
    for the start time known at that cutoff. Realized weather is outcome analysis only."""
    if realized:
        return False
    return issued_at <= as_of and valid_from <= scheduled_start < valid_to
