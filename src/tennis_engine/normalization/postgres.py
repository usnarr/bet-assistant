"""PostgreSQL implementation of the F04 ``IdentityStore`` (migrations 0004 and 0009).

It follows ``MemoryIdentityStore`` exactly: the same checks, the same idempotent adds and
the same errors (``KeyError`` for a missing entity, ``ValueError`` for a version gap).
Versioned writes take a transaction-scoped advisory lock on their key, so concurrent
writers cannot skip or duplicate a version. The tables reject UPDATE and DELETE.

Inside ``transaction()`` every method on the same thread uses one connection. Each write
runs in its own savepoint, so a rejected write leaves the transaction usable.
"""

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from tennis_engine.contracts.domain import Availability, Tour

from .contracts import (
    BestOf,
    CompetitionLevel,
    CourtEnvironment,
    DecidingSetRule,
    DrawStage,
    DrawType,
    EditionFormatVersion,
    EvidenceKind,
    Handedness,
    Match,
    MatchAlias,
    MatchStatus,
    Player,
    PlayerAlias,
    RankingSnapshot,
    ResolutionDecision,
    ResultVersion,
    ReviewState,
    Round,
    ScheduleVersion,
    ServeReturnCounts,
    SetScore,
    StatsVersion,
    StatusVersion,
    Surface,
    Tournament,
    TournamentEdition,
)
from .names import blocking_keys
from .store import AuditEntry, ReviewItem, ReviewKind, TournamentAlias

AVAILABILITY = (
    "observed_at, ingested_at, effective_at, source_available_at, "
    "availability_evidence_id, source_id"
)
AVAILABILITY_VALUES = (
    ":observed_at, :ingested_at, :effective_at, :source_available_at, "
    ":availability_evidence_id, :source_id"
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _availability(availability: Availability, source_id: str) -> dict[str, Any]:
    return {
        "observed_at": availability.observed_at,
        "ingested_at": availability.ingested_at,
        "effective_at": availability.effective_at,
        "source_available_at": availability.source_available_at,
        "availability_evidence_id": availability.availability_evidence_id,
        "source_id": source_id,
    }


def _read_availability(row: Any) -> Availability:
    return Availability(
        observed_at=row.observed_at,
        ingested_at=row.ingested_at,
        effective_at=row.effective_at,
        source_available_at=row.source_available_at,
        availability_evidence_id=row.availability_evidence_id,
    )


def _lock(db: Connection, *parts: object) -> None:
    key = ":".join(str(part) for part in parts)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


def _check_next(db: Connection, sql: str, params: dict[str, Any], proposed: int) -> None:
    current = db.execute(text(sql), params).scalar_one()
    expected = int(current or 0) + 1
    if proposed != expected:
        raise ValueError(f"Version must be {expected}, got {proposed}")


def _player(row: Any) -> Player:
    return Player(
        player_id=row.player_id,
        tour=Tour(row.tour),
        display_name=row.display_name,
        birth_date=row.birth_date,
        nationality=row.nationality,
        handedness=Handedness(row.handedness),
        created_at=row.created_at,
    )


def _player_alias(row: Any) -> PlayerAlias:
    return PlayerAlias(
        alias_id=row.alias_id,
        player_id=row.player_id,
        source_id=row.source_id,
        source_player_id=row.source_player_id,
        source_name=row.source_name,
        version=row.version,
        active=row.active,
        decision=ResolutionDecision(row.decision),
        reviewed_by=row.reviewed_by,
        evidence=tuple(EvidenceKind(item) for item in row.evidence),
        recorded_at=row.recorded_at,
        supersedes=row.supersedes,
    )


def _match(row: Any) -> Match:
    return Match(
        match_id=row.match_id,
        edition_id=row.edition_id,
        tour=Tour(row.tour),
        draw_type=DrawType(row.draw_type),
        draw_stage=DrawStage(row.draw_stage),
        round=Round(row.round),
        best_of=BestOf(row.best_of),
        player_ids=(row.player_one_id, row.player_two_id),
        created_at=row.created_at,
    )


def _match_alias(row: Any) -> MatchAlias:
    return MatchAlias(
        alias_id=row.alias_id,
        match_id=row.match_id,
        source_id=row.source_id,
        source_match_id=row.source_match_id,
        version=row.version,
        active=row.active,
        decision=ResolutionDecision(row.decision),
        reviewed_by=row.reviewed_by,
        swapped=row.swapped,
        recorded_at=row.recorded_at,
        supersedes=row.supersedes,
    )


def _review(row: Any) -> ReviewItem:
    return ReviewItem(
        review_id=row.review_id,
        revision=row.revision,
        kind=ReviewKind(row.kind),
        source_id=row.source_id,
        source_key=row.source_key,
        state=ReviewState(row.state),
        reasons=tuple(row.reasons),
        payload=row.payload,
        recorded_at=row.recorded_at,
        actor=row.actor,
        resolution_note=row.resolution_note,
    )


class PostgresIdentityStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._local = threading.local()

    def _shared(self) -> Connection | None:
        db: Connection | None = getattr(self._local, "connection", None)
        return db

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit all writes of the block together. An inner block joins the outer one."""
        if self._shared() is not None:
            yield
            return
        with self._write() as db:
            self._local.connection = db
            try:
                yield
            finally:
                self._local.connection = None

    @contextmanager
    def _write(self) -> Iterator[Connection]:
        db = self._shared()
        if db is None:
            with self.engine.begin() as own:
                yield own
            return
        with db.begin_nested():
            yield db

    @contextmanager
    def _read(self) -> Iterator[Connection]:
        db = self._shared()
        if db is None:
            with self.engine.connect() as own:
                yield own
            return
        yield db

    # Players and aliases.

    def _index(self, db: Connection, player_id: UUID, name: str) -> None:
        for key in sorted(blocking_keys(name)):
            db.execute(
                text(
                    "INSERT INTO tennis.player_name_key (name_key, player_id) "
                    "VALUES (:key, :player_id) ON CONFLICT DO NOTHING"
                ),
                {"key": key, "player_id": player_id},
            )

    def add_player(self, player: Player) -> Player:
        with self._write() as db:
            inserted = db.execute(
                text(
                    "INSERT INTO tennis.player (player_id, tour, display_name, birth_date, "
                    "nationality, handedness, created_at) VALUES (:player_id, :tour, :name, "
                    ":birth_date, :nationality, :handedness, :created_at) "
                    "ON CONFLICT (player_id) DO NOTHING"
                ),
                {
                    "player_id": player.player_id,
                    "tour": player.tour.value,
                    "name": player.display_name,
                    "birth_date": player.birth_date,
                    "nationality": player.nationality,
                    "handedness": player.handedness.value,
                    "created_at": player.created_at,
                },
            )
            if inserted.rowcount:
                self._index(db, player.player_id, player.display_name)
                return player
        return self.player(player.player_id)

    def player(self, player_id: UUID) -> Player:
        with self._read() as db:
            row = db.execute(
                text("SELECT * FROM tennis.player WHERE player_id = :id"), {"id": player_id}
            ).one_or_none()
        if row is None:
            raise KeyError(player_id)
        return _player(row)

    def players(self) -> Sequence[Player]:
        with self._read() as db:
            rows = db.execute(
                text("SELECT * FROM tennis.player ORDER BY created_at, player_id::text")
            )
            return tuple(_player(row) for row in rows)

    def append_player_alias(self, alias: PlayerAlias) -> PlayerAlias:
        with self._write() as db:
            exists = db.execute(
                text("SELECT 1 FROM tennis.player WHERE player_id = :id"),
                {"id": alias.player_id},
            ).one_or_none()
            if exists is None:
                raise ValueError("An alias requires an existing canonical player")
            _lock(db, "player_alias", alias.source_id, alias.source_player_id)
            _check_next(
                db,
                "SELECT max(version) FROM tennis.player_alias "
                "WHERE source_id = :source AND source_player_id = :key",
                {"source": alias.source_id, "key": alias.source_player_id},
                alias.version,
            )
            db.execute(
                text(
                    "INSERT INTO tennis.player_alias (alias_id, player_id, source_id, "
                    "source_player_id, source_name, version, active, decision, reviewed_by, "
                    "evidence, recorded_at, supersedes) VALUES (:alias_id, :player_id, "
                    ":source_id, :source_player_id, :source_name, :version, :active, "
                    ":decision, :reviewed_by, CAST(:evidence AS JSONB), :recorded_at, "
                    ":supersedes)"
                ),
                {
                    "alias_id": alias.alias_id,
                    "player_id": alias.player_id,
                    "source_id": alias.source_id,
                    "source_player_id": alias.source_player_id,
                    "source_name": alias.source_name,
                    "version": alias.version,
                    "active": alias.active,
                    "decision": alias.decision.value,
                    "reviewed_by": alias.reviewed_by,
                    "evidence": _json([item.value for item in alias.evidence]),
                    "recorded_at": alias.recorded_at,
                    "supersedes": alias.supersedes,
                },
            )
            self._index(db, alias.player_id, alias.source_name)
        return alias

    def player_alias(
        self, source_id: str, source_player_id: str, *, as_of: datetime | None = None
    ) -> PlayerAlias | None:
        with self._read() as db:
            row = db.execute(
                text(
                    "SELECT * FROM tennis.player_alias WHERE source_id = :source "
                    "AND source_player_id = :key "
                    "AND (CAST(:as_of AS TIMESTAMPTZ) IS NULL OR recorded_at <= :as_of) "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"source": source_id, "key": source_player_id, "as_of": as_of},
            ).one_or_none()
        if row is None or not row.active:
            return None
        return _player_alias(row)

    def player_alias_history(self, source_id: str, source_player_id: str) -> Sequence[PlayerAlias]:
        with self._read() as db:
            rows = db.execute(
                text(
                    "SELECT * FROM tennis.player_alias WHERE source_id = :source "
                    "AND source_player_id = :key ORDER BY version"
                ),
                {"source": source_id, "key": source_player_id},
            )
            return tuple(_player_alias(row) for row in rows)

    def aliases_for_player(self, player_id: UUID) -> Sequence[PlayerAlias]:
        with self._read() as db:
            rows = db.execute(
                text(
                    "SELECT * FROM tennis.player_alias WHERE player_id = :id "
                    "ORDER BY recorded_at, source_id, source_player_id, version"
                ),
                {"id": player_id},
            )
            return tuple(_player_alias(row) for row in rows)

    def candidate_player_ids(self, name: str) -> set[UUID]:
        keys = sorted(blocking_keys(name))
        if not keys:
            return set()
        with self._read() as db:
            rows = db.execute(
                text(
                    "SELECT DISTINCT player_id FROM tennis.player_name_key "
                    "WHERE name_key = ANY(:keys)"
                ),
                {"keys": keys},
            )
            return {row.player_id for row in rows}

    # Tournaments.

    def add_tournament(self, tournament: Tournament) -> Tournament:
        with self._write() as db:
            db.execute(
                text(
                    "INSERT INTO tennis.tournament (tournament_id, tour, name, level) "
                    "VALUES (:id, :tour, :name, :level) ON CONFLICT (tournament_id) DO NOTHING"
                ),
                {
                    "id": tournament.tournament_id,
                    "tour": tournament.tour.value,
                    "name": tournament.name,
                    "level": tournament.level.value,
                },
            )
        return self.tournament(tournament.tournament_id)

    def tournament(self, tournament_id: UUID) -> Tournament:
        with self._read() as db:
            row = db.execute(
                text("SELECT * FROM tennis.tournament WHERE tournament_id = :id"),
                {"id": tournament_id},
            ).one_or_none()
        if row is None:
            raise KeyError(tournament_id)
        return Tournament(
            tournament_id=row.tournament_id,
            tour=Tour(row.tour),
            name=row.name,
            level=CompetitionLevel(row.level),
        )

    def add_edition(self, edition: TournamentEdition) -> TournamentEdition:
        with self._write() as db:
            db.execute(
                text(
                    "INSERT INTO tennis.tournament_edition (edition_id, tournament_id, season, "
                    "surface, environment, timezone, start_date, end_date) VALUES (:id, "
                    ":tournament_id, :season, :surface, :environment, :timezone, :start_date, "
                    ":end_date) ON CONFLICT (edition_id) DO NOTHING"
                ),
                {
                    "id": edition.edition_id,
                    "tournament_id": edition.tournament_id,
                    "season": edition.season,
                    "surface": edition.surface.value,
                    "environment": edition.environment.value,
                    "timezone": edition.timezone,
                    "start_date": edition.start_date,
                    "end_date": edition.end_date,
                },
            )
        return self.edition(edition.edition_id)

    def edition(self, edition_id: UUID) -> TournamentEdition:
        with self._read() as db:
            row = db.execute(
                text("SELECT * FROM tennis.tournament_edition WHERE edition_id = :id"),
                {"id": edition_id},
            ).one_or_none()
        if row is None:
            raise KeyError(edition_id)
        return TournamentEdition(
            edition_id=row.edition_id,
            tournament_id=row.tournament_id,
            season=row.season,
            surface=Surface(row.surface),
            environment=CourtEnvironment(row.environment),
            timezone=row.timezone,
            start_date=row.start_date,
            end_date=row.end_date,
        )

    def add_tournament_alias(self, alias: TournamentAlias) -> TournamentAlias:
        with self._write() as db:
            edition = db.execute(
                text("SELECT tournament_id FROM tennis.tournament_edition WHERE edition_id = :id"),
                {"id": alias.edition_id},
            ).one_or_none()
            if edition is None or edition.tournament_id != alias.tournament_id:
                raise ValueError("A tournament alias requires its existing edition")
            db.execute(
                text(
                    "INSERT INTO tennis.tournament_alias (source_id, source_tournament_id, "
                    "season, edition_id, recorded_at) VALUES (:source, :key, :season, "
                    ":edition_id, :recorded_at) ON CONFLICT DO NOTHING"
                ),
                {
                    "source": alias.source_id,
                    "key": alias.source_tournament_id,
                    "season": alias.season,
                    "edition_id": alias.edition_id,
                    "recorded_at": alias.recorded_at,
                },
            )
        stored = self.tournament_alias(alias.source_id, alias.source_tournament_id, alias.season)
        if stored is None:
            raise AssertionError("The tournament alias was not stored")
        return stored

    def tournament_alias(
        self, source_id: str, source_tournament_id: str, season: int
    ) -> TournamentAlias | None:
        with self._read() as db:
            row = db.execute(
                text(
                    "SELECT a.*, e.tournament_id FROM tennis.tournament_alias a "
                    "JOIN tennis.tournament_edition e ON e.edition_id = a.edition_id "
                    "WHERE a.source_id = :source AND a.source_tournament_id = :key "
                    "AND a.season = :season"
                ),
                {"source": source_id, "key": source_tournament_id, "season": season},
            ).one_or_none()
        if row is None:
            return None
        return TournamentAlias(
            source_id=row.source_id,
            source_tournament_id=row.source_tournament_id,
            season=row.season,
            tournament_id=row.tournament_id,
            edition_id=row.edition_id,
            recorded_at=row.recorded_at,
        )

    # Matches and aliases.

    def add_match(self, match: Match) -> Match:
        with self._write() as db:
            db.execute(
                text(
                    "INSERT INTO tennis.canonical_match (match_id, edition_id, tour, draw_type, "
                    "draw_stage, round, best_of, player_one_id, player_two_id, created_at) "
                    "VALUES (:id, :edition_id, :tour, :draw_type, :draw_stage, :round, "
                    ":best_of, :one, :two, :created_at) ON CONFLICT (match_id) DO NOTHING"
                ),
                {
                    "id": match.match_id,
                    "edition_id": match.edition_id,
                    "tour": match.tour.value,
                    "draw_type": match.draw_type.value,
                    "draw_stage": match.draw_stage.value,
                    "round": match.round.value,
                    "best_of": match.best_of.value,
                    "one": match.player_ids[0],
                    "two": match.player_ids[1],
                    "created_at": match.created_at,
                },
            )
        return self.match(match.match_id)

    def match(self, match_id: UUID) -> Match:
        with self._read() as db:
            row = db.execute(
                text("SELECT * FROM tennis.canonical_match WHERE match_id = :id"),
                {"id": match_id},
            ).one_or_none()
        if row is None:
            raise KeyError(match_id)
        return _match(row)

    def matches(self) -> Sequence[Match]:
        with self._read() as db:
            rows = db.execute(
                text("SELECT * FROM tennis.canonical_match ORDER BY created_at, match_id::text")
            )
            return tuple(_match(row) for row in rows)

    def append_match_alias(self, alias: MatchAlias) -> MatchAlias:
        with self._write() as db:
            exists = db.execute(
                text("SELECT 1 FROM tennis.canonical_match WHERE match_id = :id"),
                {"id": alias.match_id},
            ).one_or_none()
            if exists is None:
                raise ValueError("A match alias requires an existing canonical match")
            _lock(db, "match_alias", alias.source_id, alias.source_match_id)
            _check_next(
                db,
                "SELECT max(version) FROM tennis.match_alias "
                "WHERE source_id = :source AND source_match_id = :key",
                {"source": alias.source_id, "key": alias.source_match_id},
                alias.version,
            )
            db.execute(
                text(
                    "INSERT INTO tennis.match_alias (alias_id, match_id, source_id, "
                    "source_match_id, version, active, decision, reviewed_by, swapped, "
                    "recorded_at, supersedes) VALUES (:alias_id, :match_id, :source_id, "
                    ":source_match_id, :version, :active, :decision, :reviewed_by, :swapped, "
                    ":recorded_at, :supersedes)"
                ),
                {
                    "alias_id": alias.alias_id,
                    "match_id": alias.match_id,
                    "source_id": alias.source_id,
                    "source_match_id": alias.source_match_id,
                    "version": alias.version,
                    "active": alias.active,
                    "decision": alias.decision.value,
                    "reviewed_by": alias.reviewed_by,
                    "swapped": alias.swapped,
                    "recorded_at": alias.recorded_at,
                    "supersedes": alias.supersedes,
                },
            )
        return alias

    def match_alias(self, source_id: str, source_match_id: str) -> MatchAlias | None:
        with self._read() as db:
            row = db.execute(
                text(
                    "SELECT * FROM tennis.match_alias WHERE source_id = :source "
                    "AND source_match_id = :key ORDER BY version DESC LIMIT 1"
                ),
                {"source": source_id, "key": source_match_id},
            ).one_or_none()
        if row is None or not row.active:
            return None
        return _match_alias(row)

    def match_alias_sources(self, match_id: UUID) -> set[str]:
        with self._read() as db:
            rows = db.execute(
                text("SELECT DISTINCT source_id FROM tennis.match_alias WHERE match_id = :id"),
                {"id": match_id},
            )
            return {row.source_id for row in rows}

    # Versioned match facts.

    def append_schedule(self, version: ScheduleVersion) -> ScheduleVersion:
        self._write_fact(
            "match_schedule_version",
            version.match_id,
            version.version,
            {"scheduled_start": version.scheduled_start},
            version.availability,
            version.source_id,
        )
        return version

    def append_status(self, version: StatusVersion) -> StatusVersion:
        self._write_fact(
            "match_status_version",
            version.match_id,
            version.version,
            {
                "status": version.status.value,
                "actual_start": version.actual_start,
                "actual_end": version.actual_end,
            },
            version.availability,
            version.source_id,
        )
        return version

    def append_result(self, version: ResultVersion) -> ResultVersion:
        self._write_fact(
            "match_result_version",
            version.match_id,
            version.version,
            {
                "status": version.status.value,
                "winner_id": version.winner_id,
                "sets": _json([item.model_dump(mode="json") for item in version.sets]),
                "corrects_version": version.corrects_version,
            },
            version.availability,
            version.source_id,
            casts={"sets"},
        )
        return version

    def _write_fact(
        self,
        table: str,
        match_id: UUID,
        version: int,
        columns: dict[str, Any],
        availability: Availability,
        source_id: str,
        casts: set[str] | None = None,
    ) -> None:
        names = ", ".join(columns)
        values = ", ".join(
            f"CAST(:{name} AS JSONB)" if name in (casts or set()) else f":{name}"
            for name in columns
        )
        with self._write() as db:
            _lock(db, table, match_id)
            _check_next(
                db,
                f"SELECT max(version) FROM tennis.{table} WHERE match_id = :match_id",
                {"match_id": match_id},
                version,
            )
            db.execute(
                text(
                    f"INSERT INTO tennis.{table} (match_id, version, {names}, {AVAILABILITY}) "
                    f"VALUES (:match_id, :version, {values}, {AVAILABILITY_VALUES})"
                ),
                {
                    "match_id": match_id,
                    "version": version,
                    **columns,
                    **_availability(availability, source_id),
                },
            )

    def append_stats(self, version: StatsVersion) -> StatsVersion:
        with self._write() as db:
            _lock(db, "match_stats_version", version.match_id, version.player_id)
            _check_next(
                db,
                "SELECT max(version) FROM tennis.match_stats_version "
                "WHERE match_id = :match_id AND player_id = :player_id",
                {"match_id": version.match_id, "player_id": version.player_id},
                version.version,
            )
            db.execute(
                text(
                    "INSERT INTO tennis.match_stats_version (match_id, player_id, version, "
                    f"counts, {AVAILABILITY}) VALUES (:match_id, :player_id, :version, "
                    f"CAST(:counts AS JSONB), {AVAILABILITY_VALUES})"
                ),
                {
                    "match_id": version.match_id,
                    "player_id": version.player_id,
                    "version": version.version,
                    "counts": _json(version.counts.model_dump(mode="json")),
                    **_availability(version.availability, version.source_id),
                },
            )
        return version

    def _facts(self, table: str, where: str, params: dict[str, Any]) -> list[Any]:
        with self._read() as db:
            return list(
                db.execute(
                    text(f"SELECT * FROM tennis.{table} WHERE {where} ORDER BY version"), params
                )
            )

    def schedules(self, match_id: UUID) -> Sequence[ScheduleVersion]:
        return tuple(
            ScheduleVersion(
                match_id=row.match_id,
                version=row.version,
                scheduled_start=row.scheduled_start,
                source_id=row.source_id,
                availability=_read_availability(row),
            )
            for row in self._facts("match_schedule_version", "match_id = :id", {"id": match_id})
        )

    def statuses(self, match_id: UUID) -> Sequence[StatusVersion]:
        return tuple(
            StatusVersion(
                match_id=row.match_id,
                version=row.version,
                status=MatchStatus(row.status),
                actual_start=row.actual_start,
                actual_end=row.actual_end,
                source_id=row.source_id,
                availability=_read_availability(row),
            )
            for row in self._facts("match_status_version", "match_id = :id", {"id": match_id})
        )

    def results(self, match_id: UUID) -> Sequence[ResultVersion]:
        return tuple(
            ResultVersion(
                match_id=row.match_id,
                version=row.version,
                status=MatchStatus(row.status),
                winner_id=row.winner_id,
                sets=tuple(SetScore.model_validate(item) for item in row.sets),
                source_id=row.source_id,
                availability=_read_availability(row),
                corrects_version=row.corrects_version,
            )
            for row in self._facts("match_result_version", "match_id = :id", {"id": match_id})
        )

    def stats(self, match_id: UUID, player_id: UUID) -> Sequence[StatsVersion]:
        return tuple(
            StatsVersion(
                match_id=row.match_id,
                player_id=row.player_id,
                version=row.version,
                counts=ServeReturnCounts.model_validate(row.counts),
                source_id=row.source_id,
                availability=_read_availability(row),
            )
            for row in self._facts(
                "match_stats_version",
                "match_id = :match_id AND player_id = :player_id",
                {"match_id": match_id, "player_id": player_id},
            )
        )

    # Deciding-set rules.

    def append_edition_format(self, version: EditionFormatVersion) -> EditionFormatVersion:
        with self._write() as db:
            exists = db.execute(
                text("SELECT 1 FROM tennis.tournament_edition WHERE edition_id = :id"),
                {"id": version.edition_id},
            ).one_or_none()
            if exists is None:
                raise ValueError("A format rule requires an existing edition")
            key = {
                "edition_id": version.edition_id,
                "draw_stage": version.draw_stage.value,
                "best_of": version.best_of.value,
            }
            _lock(db, "edition_format_version", *key.values())
            _check_next(
                db,
                "SELECT max(version) FROM tennis.edition_format_version WHERE "
                "edition_id = :edition_id AND draw_stage = :draw_stage AND best_of = :best_of",
                key,
                version.version,
            )
            db.execute(
                text(
                    "INSERT INTO tennis.edition_format_version (edition_id, draw_stage, "
                    f"best_of, version, deciding_set, reference, corrects_version, {AVAILABILITY}"
                    ") VALUES (:edition_id, :draw_stage, :best_of, :version, :deciding_set, "
                    f":reference, :corrects_version, {AVAILABILITY_VALUES})"
                ),
                {
                    **key,
                    "version": version.version,
                    "deciding_set": version.deciding_set.value,
                    "reference": version.reference,
                    "corrects_version": version.corrects_version,
                    **_availability(version.availability, version.source_id),
                },
            )
        return version

    def edition_formats(
        self, edition_id: UUID, draw_stage: DrawStage, best_of: BestOf
    ) -> Sequence[EditionFormatVersion]:
        return tuple(
            EditionFormatVersion(
                edition_id=row.edition_id,
                draw_stage=DrawStage(row.draw_stage),
                best_of=BestOf(row.best_of),
                version=row.version,
                deciding_set=DecidingSetRule(row.deciding_set),
                reference=row.reference,
                source_id=row.source_id,
                availability=_read_availability(row),
                corrects_version=row.corrects_version,
            )
            for row in self._facts(
                "edition_format_version",
                "edition_id = :edition_id AND draw_stage = :draw_stage AND best_of = :best_of",
                {
                    "edition_id": edition_id,
                    "draw_stage": draw_stage.value,
                    "best_of": best_of.value,
                },
            )
        )

    # Rankings.

    def add_ranking(self, snapshot: RankingSnapshot) -> tuple[RankingSnapshot, bool]:
        params = {
            "player_id": snapshot.player_id,
            "source_id": snapshot.source_id,
            "ranking_date": snapshot.ranking_date,
            "rank": snapshot.rank,
            "points": snapshot.points,
        }
        with self._write() as db:
            _lock(db, "ranking_snapshot", snapshot.player_id)
            existing = db.execute(
                text(
                    "SELECT * FROM tennis.ranking_snapshot WHERE player_id = :player_id "
                    "AND source_id = :source_id AND ranking_date = :ranking_date "
                    "AND rank = :rank AND points IS NOT DISTINCT FROM :points "
                    "ORDER BY snapshot_id LIMIT 1"
                ),
                params,
            ).one_or_none()
            if existing is not None:
                return self._ranking(existing), False
            db.execute(
                text(
                    "INSERT INTO tennis.ranking_snapshot (player_id, tour, ranking_date, rank, "
                    f"points, {AVAILABILITY}) VALUES (:player_id, :tour, :ranking_date, :rank, "
                    f":points, {AVAILABILITY_VALUES})"
                ),
                {
                    **params,
                    "tour": snapshot.tour.value,
                    **_availability(snapshot.availability, snapshot.source_id),
                },
            )
        return snapshot, True

    @staticmethod
    def _ranking(row: Any) -> RankingSnapshot:
        return RankingSnapshot(
            player_id=row.player_id,
            tour=Tour(row.tour),
            ranking_date=row.ranking_date,
            rank=row.rank,
            points=row.points,
            source_id=row.source_id,
            availability=_read_availability(row),
        )

    def rankings(self, player_id: UUID) -> Sequence[RankingSnapshot]:
        with self._read() as db:
            rows = db.execute(
                text(
                    "SELECT * FROM tennis.ranking_snapshot WHERE player_id = :id "
                    "ORDER BY snapshot_id"
                ),
                {"id": player_id},
            )
            return tuple(self._ranking(row) for row in rows)

    # Review queue, audit and checkpoints.

    def append_review(self, item: ReviewItem) -> ReviewItem:
        with self._write() as db:
            _lock(db, "identity_review", item.review_id)
            current = db.execute(
                text(
                    "SELECT max(revision) FROM tennis.identity_review_revision "
                    "WHERE review_id = :id"
                ),
                {"id": item.review_id},
            ).scalar_one()
            if item.revision != int(current or 0) + 1:
                raise ValueError("Review revision conflict")
            db.execute(
                text(
                    "INSERT INTO tennis.identity_review_revision (review_id, revision, kind, "
                    "source_id, source_key, state, reasons, payload, recorded_at, actor, "
                    "resolution_note) VALUES (:review_id, :revision, :kind, :source_id, "
                    ":source_key, :state, CAST(:reasons AS JSONB), CAST(:payload AS JSONB), "
                    ":recorded_at, :actor, :note)"
                ),
                {
                    "review_id": item.review_id,
                    "revision": item.revision,
                    "kind": item.kind.value,
                    "source_id": item.source_id,
                    "source_key": item.source_key,
                    "state": item.state.value,
                    "reasons": _json(list(item.reasons)),
                    "payload": _json(item.payload),
                    "recorded_at": item.recorded_at,
                    "actor": item.actor,
                    "note": item.resolution_note,
                },
            )
        return item

    def review(self, review_id: UUID) -> ReviewItem:
        with self._read() as db:
            row = db.execute(
                text(
                    "SELECT * FROM tennis.identity_review_revision WHERE review_id = :id "
                    "ORDER BY revision DESC LIMIT 1"
                ),
                {"id": review_id},
            ).one_or_none()
        if row is None:
            raise KeyError(review_id)
        return _review(row)

    def reviews(self, state: ReviewState | None = None) -> Sequence[ReviewItem]:
        with self._read() as db:
            rows = db.execute(
                text(
                    "SELECT latest.* FROM (SELECT DISTINCT ON (review_id) * "
                    "FROM tennis.identity_review_revision ORDER BY review_id, revision DESC) "
                    "latest JOIN tennis.identity_review_revision first "
                    "ON first.review_id = latest.review_id AND first.revision = 1 "
                    "WHERE CAST(:state AS TEXT) IS NULL OR latest.state = :state "
                    "ORDER BY first.recorded_at, latest.review_id::text"
                ),
                {"state": None if state is None else state.value},
            )
            return tuple(_review(row) for row in rows)

    def audit(self, entry: AuditEntry) -> None:
        with self._write() as db:
            db.execute(
                text(
                    "INSERT INTO tennis.identity_audit (recorded_at, actor, action, subject, "
                    "reason, details) VALUES (:recorded_at, :actor, :action, :subject, "
                    ":reason, CAST(:details AS JSONB))"
                ),
                {
                    "recorded_at": entry.recorded_at,
                    "actor": entry.actor,
                    "action": entry.action,
                    "subject": entry.subject,
                    "reason": entry.reason,
                    "details": _json(entry.details),
                },
            )

    def audit_log(self) -> Sequence[AuditEntry]:
        with self._read() as db:
            rows = db.execute(text("SELECT * FROM tennis.identity_audit ORDER BY audit_id"))
            return tuple(
                AuditEntry(
                    recorded_at=row.recorded_at,
                    actor=row.actor,
                    action=row.action,
                    subject=row.subject,
                    reason=row.reason,
                    details=row.details,
                )
                for row in rows
            )

    def checkpoint(self, name: str) -> int:
        with self._read() as db:
            position = db.execute(
                text("SELECT position FROM tennis.backfill_checkpoint WHERE name = :name"),
                {"name": name},
            ).scalar_one_or_none()
        return int(position or 0)

    def save_checkpoint(self, name: str, position: int) -> None:
        with self._write() as db:
            _lock(db, "backfill_checkpoint", name)
            current = db.execute(
                text("SELECT position FROM tennis.backfill_checkpoint WHERE name = :name"),
                {"name": name},
            ).scalar_one_or_none()
            if position < int(current or 0):
                raise ValueError("A checkpoint cannot move backwards")
            db.execute(
                text(
                    "INSERT INTO tennis.backfill_checkpoint (name, position, updated_at) "
                    "VALUES (:name, :position, now()) ON CONFLICT (name) DO UPDATE "
                    "SET position = EXCLUDED.position, updated_at = EXCLUDED.updated_at"
                ),
                {"name": name, "position": position},
            )
