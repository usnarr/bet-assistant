from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from settlement_support import (
    BET_TIME,
    DOCUMENT,
    REVIEWED_AT,
    payout_rule,
    registry,
    rule_documents,
    settlement_rule,
)

from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.settlement.rules import (
    RULE_LIST,
    BookmakerPayoutRule,
    JurisdictionPayoutRule,
    RuleReason,
    RuleRegistry,
    SettlementRule,
)


def test_repository_rule_configs_load_and_are_all_unreviewed_drafts():
    rules = RuleRegistry.from_directory(Path("configs/settlement/rules"))
    for bookmaker in ("betclic", "superbet", "fortuna"):
        for kind, name in ((BookmakerPayoutRule, "payout"), (SettlementRule, "settlement")):
            lookup = rules.lookup(
                kind,
                f"{bookmaker}-{name}-draft-2026-09-29",
                effective_at=datetime(2026, 10, 1, tzinfo=UTC),
                known_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
            assert lookup.reason == RuleReason.RULE_NOT_REVIEWED
    pl = rules.lookup(
        JurisdictionPayoutRule,
        "pl-payout-draft-2026-09-29",
        effective_at=datetime(2026, 10, 1, tzinfo=UTC),
        known_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert pl.reason == RuleReason.RULE_NOT_REVIEWED


def test_reviewed_rule_must_state_every_semantic_even_as_null():
    document = payout_rule()
    del document["maximum_cash_return"]
    with pytest.raises(ValidationError, match="must state: maximum_cash_return"):
        RULE_LIST.validate_python([document])
    sparse = settlement_rule()
    del sparse["walkover"]
    with pytest.raises(ValidationError, match="must state: walkover"):
        RULE_LIST.validate_python([sparse])


def test_reviewed_rule_requires_review_evidence_and_consistent_tax_treatment():
    with pytest.raises(ValidationError, match="reviewer"):
        RULE_LIST.validate_python([payout_rule(evidence=[])])
    with pytest.raises(ValidationError, match="exactly one jurisdiction"):
        RULE_LIST.validate_python([payout_rule(jurisdiction_rule_version=None)])
    with pytest.raises(ValidationError, match="own tax regime"):
        RULE_LIST.validate_python([payout_rule(tax_treatment="bookmaker")])
    with pytest.raises(ValidationError, match="completed matches on result"):
        RULE_LIST.validate_python(
            [
                settlement_rule(
                    completed={"action": "VOID", "rule": {"document_id": "d", "section": "s"}}
                )
            ]
        )
    with pytest.raises(ValidationError, match="never a float"):
        RULE_LIST.validate_python([payout_rule(minimum_stake=2.0)])


def test_duplicate_versions_are_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        RuleRegistry(RULE_LIST.validate_python(rule_documents() + [payout_rule()]))


@pytest.mark.parametrize(
    ("effective_at", "known_at", "reason"),
    [
        (BET_TIME, REVIEWED_AT - timedelta(seconds=1), RuleReason.RULE_NOT_KNOWN_AT_DECISION),
        (REVIEWED_AT - timedelta(days=1), BET_TIME, RuleReason.RULE_OUTSIDE_EFFECTIVE_INTERVAL),
        (BET_TIME, datetime(2027, 9, 1, tzinfo=UTC), RuleReason.RULE_REVIEW_EXPIRED),
        (BET_TIME, BET_TIME, None),
    ],
)
def test_lookup_uses_effective_time_and_decision_knowledge(effective_at, known_at, reason):
    lookup = registry().lookup(
        BookmakerPayoutRule,
        "synthetic-book-payout-v1",
        effective_at=effective_at,
        known_at=known_at,
    )
    assert lookup.reason == reason
    assert lookup.ok is (reason is None)


def test_lookup_fails_closed_on_missing_kind_bookmaker_and_suspension():
    rules = registry([settlement_rule("synthetic-suspended-v1", state="SUSPENDED")])
    timing = {"effective_at": BET_TIME, "known_at": BET_TIME}
    assert rules.lookup(SettlementRule, None, **timing).reason == RuleReason.RULE_MISSING
    assert rules.lookup(SettlementRule, "nope", **timing).reason == RuleReason.RULE_MISSING
    assert (
        rules.lookup(SettlementRule, "synthetic-book-payout-v1", **timing).reason
        == RuleReason.RULE_KIND_MISMATCH
    )
    assert (
        rules.lookup(
            SettlementRule, "synthetic-book-settlement-v1", bookmaker="other", **timing
        ).reason
        == RuleReason.RULE_BOOKMAKER_MISMATCH
    )
    assert (
        rules.lookup(SettlementRule, "synthetic-suspended-v1", **timing).reason
        == RuleReason.RULE_SUSPENDED
    )


def test_lookup_verifies_archived_rule_document_through_governance(tmp_path):
    store = GovernanceStore(
        tmp_path / "governance.sqlite3",
        Principal(identity="fixture-reviewer", role=Role.POLICY_REVIEWER),
        lambda: REVIEWED_AT - timedelta(days=1),
    )
    try:
        rules = registry(evidence_checker=store.evidence_problem)
        timing = {"effective_at": BET_TIME, "known_at": BET_TIME}
        missing = rules.lookup(BookmakerPayoutRule, "synthetic-book-payout-v1", **timing)
        assert missing.reason == RuleReason.RULE_EVIDENCE_PROBLEM
        assert missing.detail == "EVIDENCE_MISSING"
        store.archive_document(
            "synthetic-rules-doc",
            "payout:rule:synthetic-book-payout-v1",
            "synthetic://F06",
            DOCUMENT,
            reason="F06 synthetic rule evidence",
        )
        assert rules.lookup(BookmakerPayoutRule, "synthetic-book-payout-v1", **timing).ok
    finally:
        store.close()
