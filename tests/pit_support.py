"""Miniature point-in-time history for F07/F08 tests (synthetic, fictional players)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from tennis_engine.common.clock import FrozenClock
from tennis_engine.contracts.domain import Availability
from tennis_engine.normalization.contracts import (
    ServeReturnCounts,
    SetScore,
    SourceMatchRecord,
    SourcePlayerRecord,
    SourceRankingRecord,
    SourceTournamentRecord,
)
from tennis_engine.normalization.resolver import DEFAULT_POLICY, EvidenceResolver
from tennis_engine.normalization.store import MemoryIdentityStore
from tennis_engine.normalization.warehouse import SportsWarehouse

SOURCE = "synthetic-sports"
SETUP = datetime(2025, 1, 1, tzinfo=UTC)
POLICY = DEFAULT_POLICY.model_copy(update={"allow_create": frozenset({SOURCE})})
WIN = ({"games": (6, 4)}, {"games": (6, 4)})


def available(observed: datetime, **extra) -> Availability:
    return Availability(observed_at=observed, ingested_at=observed, **extra)


@dataclass
class History:
    clock: FrozenClock
    store: MemoryIdentityStore
    warehouse: SportsWarehouse

    def player(self, key: str, *, tour="ATP", birth: date | None = None):
        self.warehouse.ingest_player(
            SourcePlayerRecord(
                source_id=SOURCE,
                source_player_id=key,
                full_name=f"{key.title()} {key.title()}sen",
                tour=tour,
                birth_date=birth or date(1990 + len(self.store.players()), 1, 1),
            )
        )
        return self.pid(key)

    def pid(self, key: str):
        alias = self.store.player_alias(SOURCE, key)
        assert alias is not None
        return alias.player_id

    def tournament(self, key="t", *, tour="ATP", surface="HARD", season=2026):
        self.warehouse.ingest_tournament(
            SourceTournamentRecord(
                source_id=SOURCE,
                source_tournament_id=key,
                name=f"Fixture {key}",
                season=season,
                tour=tour,
                surface=surface,
                environment="OUTDOOR",
                timezone="Europe/Warsaw",
            )
        )

    def match(
        self,
        key: str,
        first: str,
        second: str,
        *,
        start: datetime,
        observed: datetime,
        winner: str | None = None,
        sets=WIN,
        status: str | None = None,
        tournament="t",
        tour="ATP",
        season=2026,
        stats=(None, None),
        best_of="BEST_OF_3",
        **availability,
    ):
        finished = winner is not None
        if sets is WIN and winner == second:
            sets = ({"games": (4, 6)}, {"games": (4, 6)})
        record = SourceMatchRecord(
            source_id=SOURCE,
            source_match_id=key,
            source_tournament_id=tournament,
            season=season,
            tour=tour,
            draw_type="SINGLES",
            draw_stage="MAIN",
            round="R32",
            best_of=best_of,
            participant_ids=(first, second),
            scheduled_start=start,
            actual_start=start if finished else None,
            actual_end=start + timedelta(hours=2) if finished else None,
            status=status or ("COMPLETED" if finished else "SCHEDULED"),
            winner_id=winner,
            sets=tuple(SetScore.model_validate(item) for item in sets) if finished else (),
            stats=tuple(
                ServeReturnCounts.model_validate(item) if item is not None else None
                for item in stats
            ),
        )
        effective = record.actual_end or start
        outcome = self.warehouse.ingest_match(
            record, available(observed, effective_at=effective, **availability)
        )
        assert outcome.accepted, outcome
        alias = self.store.match_alias(SOURCE, key)
        assert alias is not None
        return alias.match_id

    def ranking(self, key: str, rank: int, *, dated: date, observed: datetime, **extra):
        outcome = self.warehouse.ingest_ranking(
            SourceRankingRecord(
                source_id=SOURCE,
                source_player_id=key,
                tour="ATP",
                ranking_date=dated,
                rank=rank,
                points=1000,
            ),
            available(
                observed,
                effective_at=datetime.combine(dated, datetime.min.time(), tzinfo=UTC),
                **extra,
            ),
        )
        assert outcome.accepted


def history() -> History:
    clock = FrozenClock(SETUP)
    store = MemoryIdentityStore()
    warehouse = SportsWarehouse(store, EvidenceResolver(store, POLICY), clock)
    state = History(clock, store, warehouse)
    for key in ("alpha", "bravo", "charlie", "delta"):
        state.player(key)
    state.tournament()
    return state
