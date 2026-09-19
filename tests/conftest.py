from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from tennis_engine.governance.contracts import (
    EvidenceRef,
    PayoutPolicy,
    PayoutSchedule,
    Principal,
    ResponsibleUsePolicy,
    ResponsibleUseSchedule,
    Role,
    SourcePolicy,
)
from tennis_engine.governance.service import GovernanceService
from tennis_engine.governance.store import GovernanceStore


@dataclass
class Clock:
    now: datetime = datetime(2026, 9, 19, 10, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    instance = GovernanceStore(
        tmp_path / "governance.sqlite3",
        Principal(identity="fixture-reviewer", role=Role.POLICY_REVIEWER),
        clock,
    )
    yield instance
    instance.close()


@pytest.fixture
def service(store):
    return GovernanceService(store)


def reviewed(store, scope, document_id):
    _, sha256 = store.archive_document(
        document_id,
        scope,
        "synthetic://SYS-01",
        b"Synthetic fixture, not real terms or rules.",
        reason="SYS-01 synthetic evidence",
    )
    return {
        "version": "fixture-v1",
        "owner": "fixture-owner",
        "reviewer": "fixture-reviewer",
        "reviewed_at": store.clock(),
        "review_due_at": store.clock() + timedelta(days=365),
        "effective_from": store.clock(),
        "effective_until": store.clock() + timedelta(days=365),
        "evidence": (EvidenceRef(document_id=document_id, sha256=sha256),),
    }


def source_policy(store, source_id="synthetic-sports", **overrides):
    data = reviewed(store, f"source:{source_id}", f"{source_id}-terms") | {
        "source_id": source_id,
        "category": "sports",
        "access_method": "file",
        "terms_reference": "synthetic://SYS-01",
        "commercial_use": "yes",
        "allowed_purposes": ["prototype", "production"],
        "quota": {"requests": 1, "window_seconds": 1, "concurrency": 1},
        "retention": {"raw_days": 7, "basis": "Synthetic fixture license"},
        "state": "PRODUCTION_APPROVED",
        "kill_switch": False,
    }
    return SourcePolicy.model_validate(data | overrides)


def payout_policy(store, **overrides):
    return PayoutPolicy.model_validate(
        reviewed(store, "payout:synthetic-book", "synthetic-book-rules")
        | {
            "bookmaker": "synthetic-book",
            "state": "APPROVED",
            "payout_rule_version": "synthetic-payout-v1",
            "settlement_rule_version": "synthetic-settlement-v1",
        }
        | overrides
    )


def responsible_policy(store, **overrides):
    return ResponsibleUsePolicy.model_validate(
        reviewed(store, "responsible_use:shadow", "shadow-risk-review")
        | {
            "account_scope": "shadow",
            "ledger_scope": "virtual",
            "state": "APPROVED",
            "daily": {"stake": "10.00", "count": 2},
            "weekly": {"stake": "50.00", "count": 10},
            "monthly": {"stake": "100.00", "count": 20},
            "max_event_exposure": "5.00",
            "max_open_exposure": "10.00",
            "max_bankroll_fraction": "0.01",
            "drawdown_stop": "0.10",
            "disable_recommendations": False,
        }
        | overrides
    )


@pytest.fixture
def enabled(store):
    source = source_policy(store)
    source_revision = store.save(source, expected_revision=0, reason="Synthetic source approval")
    payout = payout_policy(store)
    store.save(
        PayoutSchedule(bookmaker=payout.bookmaker, policies=(payout,)),
        expected_revision=0,
        reason="Synthetic payout policy approval",
    )
    responsible = responsible_policy(store)
    store.save(
        ResponsibleUseSchedule(account_scope="shadow", policies=(responsible,)),
        expected_revision=0,
        reason="Synthetic risk policy approval",
    )
    store.set_global_disable(False, reason="Enable synthetic tests only")
    return source, source_revision, payout, responsible
