"""Shared builders for F04 identity tests (synthetic fixtures only)."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tennis_engine.common.clock import FrozenClock
from tennis_engine.infrastructure.object_store import LocalObjectStore
from tennis_engine.ingestion.contracts import FetchCapture, FetchDisposition, FetchOrigin
from tennis_engine.ingestion.service import IngestionService
from tennis_engine.ingestion.store import MemoryIngestionStore
from tennis_engine.normalization.backfill import (
    Backfill,
    SourceFact,
    SyntheticWarehouseParser,
    facts_from_ingestion,
)
from tennis_engine.normalization.contracts import (
    ResolutionPolicy,
    ReviewState,
    SourcePlayerRecord,
)
from tennis_engine.normalization.resolver import DEFAULT_POLICY, EvidenceResolver
from tennis_engine.normalization.store import AuditEntry, IdentityStore, MemoryIdentityStore
from tennis_engine.normalization.warehouse import SportsWarehouse

FIXTURES = Path(__file__).parent / "fixtures" / "identity" / "synthetic-warehouse-v1"
FIRST_OBSERVATION = datetime(2026, 9, 19, 10, tzinfo=UTC)
SECOND_OBSERVATION = datetime(2026, 9, 20, 10, tzinfo=UTC)
POLICY: ResolutionPolicy = DEFAULT_POLICY.model_copy(
    update={"allow_create": frozenset({"synthetic-sports"})}
)


def labels() -> dict:
    return json.loads((FIXTURES / "labels.json").read_text(encoding="utf-8"))


def archive_payload(
    service: IngestionService, clock: FrozenClock, name: str, observed: datetime
) -> None:
    clock.instant = observed
    body = (FIXTURES / name).read_bytes()
    result = service.archive(
        idempotency_key=f"sys-04:{name}",
        observation_window=observed,
        capture=FetchCapture(
            source_id="synthetic-sports",
            logical_resource_id="synthetic-warehouse",
            request_identity=f"FILE {name}",
            requested_at=observed,
            completed_at=observed + timedelta(seconds=1),
            origin=FetchOrigin.FILE_IMPORT,
            disposition=FetchDisposition.SUCCESS,
            attempt_number=1,
            status_code=200,
            content_type="application/json",
            body=body,
        ),
        parser_candidate=SyntheticWarehouseParser.version,
        policy_version="fixture-v1",
        policy_revision=1,
    )
    assert result.observation_id is not None
    selection = service.repository.observation(result.observation_id)
    service.parse_observation(selection, SyntheticWarehouseParser())


@dataclass
class World:
    clock: FrozenClock
    ingestion: MemoryIngestionStore
    service: IngestionService
    store: IdentityStore
    resolver: EvidenceResolver
    warehouse: SportsWarehouse
    backfill: Backfill

    def facts(self) -> list[SourceFact]:
        return facts_from_ingestion(self.ingestion, SyntheticWarehouseParser.version)

    def run(self, **kwargs):
        return self.backfill.run(
            self.facts(),
            checkpoint=kwargs.pop("checkpoint", "sys-04"),
            parser_version=SyntheticWarehouseParser.version,
            **kwargs,
        )

    def player_id(self, source_player_id: str, source_id: str = "synthetic-sports"):
        alias = self.store.player_alias(source_id, source_player_id)
        assert alias is not None, source_player_id
        return alias.player_id

    def match_id(self, source_match_id: str, source_id: str = "synthetic-sports"):
        alias = self.store.match_alias(source_id, source_match_id)
        assert alias is not None, source_match_id
        return alias.match_id


def world(
    tmp_path: Path,
    *,
    payloads: tuple[str, ...] = ("payload-1.json",),
    store: IdentityStore | None = None,
) -> World:
    clock = FrozenClock(FIRST_OBSERVATION)
    ingestion = MemoryIngestionStore()
    service = IngestionService(ingestion, LocalObjectStore(tmp_path / "objects"), clock)
    observed = {"payload-1.json": FIRST_OBSERVATION, "payload-2.json": SECOND_OBSERVATION}
    for name in payloads:
        archive_payload(service, clock, name, observed[name])
    clock.instant = max(observed[name] for name in payloads) + timedelta(hours=1)
    store = MemoryIdentityStore() if store is None else store
    resolver = EvidenceResolver(store, POLICY)
    warehouse = SportsWarehouse(store, resolver, clock)
    return World(
        clock, ingestion, service, store, resolver, warehouse, Backfill(warehouse, store, clock)
    )


def check_operations_are_atomic(state: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed operation or batch writes no rows; the same checks run for every store."""
    store = state.store
    apply = state.backfill._apply
    calls = 0

    def fail_third(fact: SourceFact):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected backfill failure")
        return apply(fact)

    monkeypatch.setattr(state.backfill, "_apply", fail_third)
    with pytest.raises(RuntimeError, match="injected"):
        state.run(batch_size=5)
    assert store.checkpoint("sys-04") == 0
    assert (store.players(), store.reviews(), store.audit_log()) == ((), (), ())
    monkeypatch.setattr(state.backfill, "_apply", apply)
    report = state.run(batch_size=5)
    assert report.start_position == 0 and store.checkpoint("sys-04") == len(state.facts())

    outcome = state.warehouse.ingest_player(
        SourcePlayerRecord(
            source_id="synthetic-stats", source_player_id="s-10", full_name="Kowalski", tour="ATP"
        )
    )
    assert outcome.review_id is not None
    audit_rows = len(store.audit_log())
    with pytest.raises(RuntimeError, match="caller failure"):
        with store.transaction():
            state.warehouse.reject_review(outcome.review_id, reviewer="fixture", reason="x")
            raise RuntimeError("caller failure")
    assert store.review(outcome.review_id).state == ReviewState.OPEN

    def fail_audit(entry: AuditEntry) -> None:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(store, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit"):
        state.warehouse.approve_player(
            outcome.review_id, reviewer="fixture", reason="x", create=True
        )
    monkeypatch.undo()
    assert store.player_alias_history("synthetic-stats", "s-10") == ()
    assert store.review(outcome.review_id).state == ReviewState.OPEN
    assert len(store.audit_log()) == audit_rows

    known = store.player_alias("synthetic-sports", "p-kowalski-j")
    assert known is not None
    with store.transaction():
        with pytest.raises(ValueError, match="Version must be"):
            store.append_player_alias(known.model_copy(update={"version": known.version + 2}))
        store.save_checkpoint("atomic-nested", 7)
    assert store.checkpoint("atomic-nested") == 7
    alias = state.warehouse.approve_player(
        outcome.review_id, reviewer="fixture", reason="x", create=True
    )
    assert alias.version == 1
    assert store.review(outcome.review_id).state == ReviewState.APPROVED
