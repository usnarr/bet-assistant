"""Append-only identity warehouse boundary and its in-memory reference implementation.

Aliases and facts are versions. A newer version never edits an older one, so F07 can
select exactly what was recorded or observed by a cutoff.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from .contracts import (
    BestOf,
    DrawStage,
    EditionFormatVersion,
    Match,
    MatchAlias,
    Player,
    PlayerAlias,
    RankingSnapshot,
    ResultVersion,
    ReviewState,
    ScheduleVersion,
    StatsVersion,
    StatusVersion,
    Tournament,
    TournamentEdition,
)
from .names import blocking_keys


class ReviewKind(StrEnum):
    PLAYER = "PLAYER"
    TOURNAMENT = "TOURNAMENT"
    MATCH = "MATCH"
    EVENT = "EVENT"
    RANKING = "RANKING"
    RECORD = "RECORD"


@dataclass(frozen=True)
class ReviewItem:
    """One revision of a review-queue entry. The latest revision is the current state."""

    review_id: UUID
    revision: int
    kind: ReviewKind
    source_id: str
    source_key: str
    state: ReviewState
    reasons: tuple[str, ...]
    payload: dict[str, Any]
    recorded_at: datetime
    actor: str
    resolution_note: str | None = None


@dataclass(frozen=True)
class AuditEntry:
    recorded_at: datetime
    actor: str
    action: str
    subject: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TournamentAlias:
    source_id: str
    source_tournament_id: str
    season: int
    tournament_id: UUID
    edition_id: UUID
    recorded_at: datetime


class IdentityStore(Protocol):
    def add_player(self, player: Player) -> Player: ...
    def player(self, player_id: UUID) -> Player: ...
    def players(self) -> Sequence[Player]: ...
    def append_player_alias(self, alias: PlayerAlias) -> PlayerAlias: ...
    def player_alias(
        self, source_id: str, source_player_id: str, *, as_of: datetime | None = None
    ) -> PlayerAlias | None: ...
    def player_alias_history(
        self, source_id: str, source_player_id: str
    ) -> Sequence[PlayerAlias]: ...
    def aliases_for_player(self, player_id: UUID) -> Sequence[PlayerAlias]: ...
    def candidate_player_ids(self, name: str) -> set[UUID]: ...
    def add_tournament(self, tournament: Tournament) -> Tournament: ...
    def add_edition(self, edition: TournamentEdition) -> TournamentEdition: ...
    def edition(self, edition_id: UUID) -> TournamentEdition: ...
    def tournament(self, tournament_id: UUID) -> Tournament: ...
    def add_tournament_alias(self, alias: TournamentAlias) -> TournamentAlias: ...
    def tournament_alias(
        self, source_id: str, source_tournament_id: str, season: int
    ) -> TournamentAlias | None: ...
    def add_match(self, match: Match) -> Match: ...
    def match(self, match_id: UUID) -> Match: ...
    def matches(self) -> Sequence[Match]: ...
    def append_match_alias(self, alias: MatchAlias) -> MatchAlias: ...
    def match_alias(self, source_id: str, source_match_id: str) -> MatchAlias | None: ...
    def match_alias_sources(self, match_id: UUID) -> set[str]: ...
    def append_schedule(self, version: ScheduleVersion) -> ScheduleVersion: ...
    def append_status(self, version: StatusVersion) -> StatusVersion: ...
    def append_result(self, version: ResultVersion) -> ResultVersion: ...
    def append_stats(self, version: StatsVersion) -> StatsVersion: ...
    def append_edition_format(self, version: EditionFormatVersion) -> EditionFormatVersion: ...
    def schedules(self, match_id: UUID) -> Sequence[ScheduleVersion]: ...
    def statuses(self, match_id: UUID) -> Sequence[StatusVersion]: ...
    def results(self, match_id: UUID) -> Sequence[ResultVersion]: ...
    def stats(self, match_id: UUID, player_id: UUID) -> Sequence[StatsVersion]: ...
    def edition_formats(
        self, edition_id: UUID, draw_stage: DrawStage, best_of: BestOf
    ) -> Sequence[EditionFormatVersion]: ...
    def add_ranking(self, snapshot: RankingSnapshot) -> tuple[RankingSnapshot, bool]: ...
    def rankings(self, player_id: UUID) -> Sequence[RankingSnapshot]: ...
    def append_review(self, item: ReviewItem) -> ReviewItem: ...
    def review(self, review_id: UUID) -> ReviewItem: ...
    def reviews(self, state: ReviewState | None = None) -> Sequence[ReviewItem]: ...
    def audit(self, entry: AuditEntry) -> None: ...
    def audit_log(self) -> Sequence[AuditEntry]: ...
    def checkpoint(self, name: str) -> int: ...
    def save_checkpoint(self, name: str, position: int) -> None: ...


def _next_version(existing: Sequence[Any], proposed: Any) -> None:
    expected = len(existing) + 1
    if proposed.version != expected:
        raise ValueError(f"Version must be {expected}, got {proposed.version}")


class MemoryIdentityStore:
    """Deterministic reference model used by unit tests and fixture backfills."""

    def __init__(self) -> None:
        self._players: dict[UUID, Player] = {}
        self._player_aliases: dict[tuple[str, str], list[PlayerAlias]] = {}
        self._aliases_by_player: dict[UUID, list[PlayerAlias]] = {}
        self._name_index: dict[str, set[UUID]] = {}
        self._tournaments: dict[UUID, Tournament] = {}
        self._editions: dict[UUID, TournamentEdition] = {}
        self._tournament_aliases: dict[tuple[str, str, int], TournamentAlias] = {}
        self._matches: dict[UUID, Match] = {}
        self._match_aliases: dict[tuple[str, str], list[MatchAlias]] = {}
        self._schedules: dict[UUID, list[ScheduleVersion]] = {}
        self._statuses: dict[UUID, list[StatusVersion]] = {}
        self._results: dict[UUID, list[ResultVersion]] = {}
        self._stats: dict[tuple[UUID, UUID], list[StatsVersion]] = {}
        self._formats: dict[tuple[UUID, DrawStage, BestOf], list[EditionFormatVersion]] = {}
        self._rankings: dict[UUID, list[RankingSnapshot]] = {}
        self._reviews: dict[UUID, list[ReviewItem]] = {}
        self._audit: list[AuditEntry] = []
        self._checkpoints: dict[str, int] = {}

    def _index(self, player_id: UUID, name: str) -> None:
        for key in blocking_keys(name):
            self._name_index.setdefault(key, set()).add(player_id)

    def add_player(self, player: Player) -> Player:
        existing = self._players.get(player.player_id)
        if existing is not None:
            return existing
        self._players[player.player_id] = player
        self._index(player.player_id, player.display_name)
        return player

    def player(self, player_id: UUID) -> Player:
        return self._players[player_id]

    def players(self) -> Sequence[Player]:
        return tuple(self._players.values())

    def append_player_alias(self, alias: PlayerAlias) -> PlayerAlias:
        if alias.player_id not in self._players:
            raise ValueError("An alias requires an existing canonical player")
        history = self._player_aliases.setdefault((alias.source_id, alias.source_player_id), [])
        _next_version(history, alias)
        history.append(alias)
        self._aliases_by_player.setdefault(alias.player_id, []).append(alias)
        self._index(alias.player_id, alias.source_name)
        return alias

    def player_alias(
        self, source_id: str, source_player_id: str, *, as_of: datetime | None = None
    ) -> PlayerAlias | None:
        history = self._player_aliases.get((source_id, source_player_id), [])
        known = [item for item in history if as_of is None or item.recorded_at <= as_of]
        if not known or not known[-1].active:
            return None
        return known[-1]

    def player_alias_history(self, source_id: str, source_player_id: str) -> Sequence[PlayerAlias]:
        return tuple(self._player_aliases.get((source_id, source_player_id), ()))

    def aliases_for_player(self, player_id: UUID) -> Sequence[PlayerAlias]:
        return tuple(self._aliases_by_player.get(player_id, ()))

    def candidate_player_ids(self, name: str) -> set[UUID]:
        found: set[UUID] = set()
        for key in blocking_keys(name):
            found |= self._name_index.get(key, set())
        return found

    def add_tournament(self, tournament: Tournament) -> Tournament:
        return self._tournaments.setdefault(tournament.tournament_id, tournament)

    def add_edition(self, edition: TournamentEdition) -> TournamentEdition:
        return self._editions.setdefault(edition.edition_id, edition)

    def edition(self, edition_id: UUID) -> TournamentEdition:
        return self._editions[edition_id]

    def tournament(self, tournament_id: UUID) -> Tournament:
        return self._tournaments[tournament_id]

    def add_tournament_alias(self, alias: TournamentAlias) -> TournamentAlias:
        key = (alias.source_id, alias.source_tournament_id, alias.season)
        return self._tournament_aliases.setdefault(key, alias)

    def tournament_alias(
        self, source_id: str, source_tournament_id: str, season: int
    ) -> TournamentAlias | None:
        return self._tournament_aliases.get((source_id, source_tournament_id, season))

    def add_match(self, match: Match) -> Match:
        return self._matches.setdefault(match.match_id, match)

    def match(self, match_id: UUID) -> Match:
        return self._matches[match_id]

    def matches(self) -> Sequence[Match]:
        return tuple(self._matches.values())

    def append_match_alias(self, alias: MatchAlias) -> MatchAlias:
        if alias.match_id not in self._matches:
            raise ValueError("A match alias requires an existing canonical match")
        history = self._match_aliases.setdefault((alias.source_id, alias.source_match_id), [])
        _next_version(history, alias)
        history.append(alias)
        return alias

    def match_alias(self, source_id: str, source_match_id: str) -> MatchAlias | None:
        history = self._match_aliases.get((source_id, source_match_id), [])
        return history[-1] if history and history[-1].active else None

    def match_alias_sources(self, match_id: UUID) -> set[str]:
        return {
            source_id
            for (source_id, _), history in self._match_aliases.items()
            if any(item.match_id == match_id for item in history)
        }

    def append_schedule(self, version: ScheduleVersion) -> ScheduleVersion:
        history = self._schedules.setdefault(version.match_id, [])
        _next_version(history, version)
        history.append(version)
        return version

    def append_status(self, version: StatusVersion) -> StatusVersion:
        history = self._statuses.setdefault(version.match_id, [])
        _next_version(history, version)
        history.append(version)
        return version

    def append_result(self, version: ResultVersion) -> ResultVersion:
        history = self._results.setdefault(version.match_id, [])
        _next_version(history, version)
        history.append(version)
        return version

    def append_stats(self, version: StatsVersion) -> StatsVersion:
        history = self._stats.setdefault((version.match_id, version.player_id), [])
        _next_version(history, version)
        history.append(version)
        return version

    def append_edition_format(self, version: EditionFormatVersion) -> EditionFormatVersion:
        if version.edition_id not in self._editions:
            raise ValueError("A format rule requires an existing edition")
        key = (version.edition_id, version.draw_stage, version.best_of)
        history = self._formats.setdefault(key, [])
        _next_version(history, version)
        history.append(version)
        return version

    def edition_formats(
        self, edition_id: UUID, draw_stage: DrawStage, best_of: BestOf
    ) -> Sequence[EditionFormatVersion]:
        return tuple(self._formats.get((edition_id, draw_stage, best_of), ()))

    def schedules(self, match_id: UUID) -> Sequence[ScheduleVersion]:
        return tuple(self._schedules.get(match_id, ()))

    def statuses(self, match_id: UUID) -> Sequence[StatusVersion]:
        return tuple(self._statuses.get(match_id, ()))

    def results(self, match_id: UUID) -> Sequence[ResultVersion]:
        return tuple(self._results.get(match_id, ()))

    def stats(self, match_id: UUID, player_id: UUID) -> Sequence[StatsVersion]:
        return tuple(self._stats.get((match_id, player_id), ()))

    def add_ranking(self, snapshot: RankingSnapshot) -> tuple[RankingSnapshot, bool]:
        history = self._rankings.setdefault(snapshot.player_id, [])
        for existing in history:
            if (
                existing.ranking_date == snapshot.ranking_date
                and existing.source_id == snapshot.source_id
                and (existing.rank, existing.points) == (snapshot.rank, snapshot.points)
            ):
                return existing, False
        history.append(snapshot)
        return snapshot, True

    def rankings(self, player_id: UUID) -> Sequence[RankingSnapshot]:
        return tuple(self._rankings.get(player_id, ()))

    def append_review(self, item: ReviewItem) -> ReviewItem:
        history = self._reviews.setdefault(item.review_id, [])
        if item.revision != len(history) + 1:
            raise ValueError("Review revision conflict")
        history.append(item)
        return item

    def review(self, review_id: UUID) -> ReviewItem:
        return self._reviews[review_id][-1]

    def reviews(self, state: ReviewState | None = None) -> Sequence[ReviewItem]:
        latest = [history[-1] for history in self._reviews.values()]
        return tuple(item for item in latest if state is None or item.state == state)

    def audit(self, entry: AuditEntry) -> None:
        self._audit.append(entry)

    def audit_log(self) -> Sequence[AuditEntry]:
        return tuple(self._audit)

    def checkpoint(self, name: str) -> int:
        return self._checkpoints.get(name, 0)

    def save_checkpoint(self, name: str, position: int) -> None:
        if position < self._checkpoints.get(name, 0):
            raise ValueError("A checkpoint cannot move backwards")
        self._checkpoints[name] = position
