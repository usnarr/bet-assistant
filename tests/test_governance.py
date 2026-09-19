import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import payout_policy, responsible_policy, source_policy
from pydantic import ValidationError

from tennis_engine.governance.contracts import (
    PayoutSchedule,
    Principal,
    Purpose,
    ResponsibleUseSchedule,
    Role,
    SourcePolicy,
)
from tennis_engine.governance.service import GovernanceService, PermissionDenied, reset_period
from tennis_engine.governance.store import ConflictError, GovernanceStore, digest


@pytest.mark.parametrize(
    "overrides,purpose,expected",
    [
        ({}, "prototype", "ALLOWED"),
        ({}, "production", "ALLOWED"),
        ({"state": "DRAFT"}, "prototype", "SOURCE_NOT_APPROVED"),
        ({"state": "SUSPENDED"}, "prototype", "SOURCE_SUSPENDED"),
        ({"kill_switch": True}, "production", "SOURCE_DISABLED"),
        (
            {"state": "PROTOTYPE_APPROVED", "allowed_purposes": ["prototype"]},
            "production",
            "PRODUCTION_APPROVAL_REQUIRED",
        ),
        (
            {"state": "PROTOTYPE_APPROVED", "allowed_purposes": ["prototype"]},
            "prototype",
            "ALLOWED",
        ),
        ({}, "redistribution", "PURPOSE_NOT_ALLOWED"),
    ],
)
def test_sys01_permission_matrix(store, service, overrides, purpose, expected):
    source = source_policy(store, **overrides)
    store.save(source, expected_revision=0, reason="SYS-01 matrix")
    result = service.can_fetch(source.source_id, Purpose(purpose))
    assert result.reason == expected
    assert result.allowed == (expected == "ALLOWED")
    assert result.recommendation == (None if result.allowed else "NO_BET")


def test_unknown_source_denied(service):
    assert service.can_fetch("missing", Purpose.PROTOTYPE).reason == "SOURCE_UNKNOWN"


@pytest.mark.parametrize("offset", [-1, 0, 59, 60])
def test_effective_interval_is_half_open(store, service, clock, offset):
    policy = source_policy(
        store,
        effective_from=clock.now + timedelta(seconds=10),
        effective_until=clock.now + timedelta(seconds=70),
    )
    store.save(policy, expected_revision=0, reason="Boundary fixture")
    clock.now += timedelta(seconds=10 + offset)
    assert service.can_fetch(policy.source_id, Purpose.PRODUCTION).allowed == (0 <= offset < 60)


def test_review_expiry_blocks_at_boundary(store, service, clock):
    policy = source_policy(store, review_due_at=clock.now + timedelta(minutes=1))
    store.save(policy, expected_revision=0, reason="Expiring review")
    clock.now += timedelta(minutes=1)
    assert service.can_fetch(policy.source_id, Purpose.PRODUCTION).reason == "REVIEW_EXPIRED"


def test_queued_job_and_publication_recheck_revocation(store, service, clock, enabled):
    policy, revision, _, _ = enabled
    ran = []
    assert service.publication_gate(
        [policy.source_id], "synthetic-book", "shadow", Purpose.PRODUCTION
    ).allowed
    service.execute_fetch(policy.source_id, Purpose.PRODUCTION, lambda: ran.append("first"))
    clock.now += timedelta(seconds=1)
    revoked = SourcePolicy.model_validate(
        policy.model_dump()
        | {
            "version": "fixture-v2",
            "kill_switch": True,
            "state": "SUSPENDED",
        }
    )
    store.save(revoked, expected_revision=revision, reason="Source revoked after job queued")
    with pytest.raises(PermissionDenied):
        service.execute_fetch(policy.source_id, Purpose.PRODUCTION, lambda: ran.append("second"))
    assert ran == ["first"]
    assert (
        service.publication_gate(
            [policy.source_id], "synthetic-book", "shadow", Purpose.PRODUCTION
        ).reason
        == "SOURCE_DISABLED"
    )


def test_document_change_blocks_payout_and_retains_as_of_history(store, service, clock, enabled):
    _, _, payout, _ = enabled
    before_change = clock.now
    clock.now += timedelta(seconds=1)
    rev, sha256 = store.archive_document(
        "synthetic-book-rules",
        "payout:synthetic-book",
        "synthetic://changed",
        b"Changed rules",
        reason="New document requires review",
    )
    assert digest(b"Changed rules") == sha256
    assert (
        store.db.execute("SELECT content FROM document_bytes WHERE revision=?", (rev,)).fetchone()[
            0
        ]
        == b"Changed rules"
    )
    assert (
        service.get_payout_policy(payout.bookmaker, before_change, clock.now).decision.reason
        == "DOCUMENT_REVIEW_REQUIRED"
    )
    historical = service.get_payout_policy(payout.bookmaker, before_change, before_change)
    assert historical.decision.allowed
    assert historical.policy.version == payout.version
    assert len(store.records("document", "synthetic-book-rules")) == 2


def test_new_communication_also_blocks_policy(store, service, clock, enabled):
    clock.now += timedelta(seconds=1)
    store.archive_document(
        "synthetic-communication",
        "payout:synthetic-book",
        "synthetic://notice",
        b"New communication",
        reason="Pending review",
    )
    assert (
        service.get_payout_policy("synthetic-book", clock.now, clock.now).decision.reason
        == "DOCUMENT_REVIEW_REQUIRED"
    )


def test_document_deduplication_does_not_invalidate_policy(store, service, enabled):
    original = store.records("document", "synthetic-book-rules")[0]
    result = store.archive_document(
        "synthetic-book-rules",
        "payout:synthetic-book",
        "synthetic://same",
        b"Synthetic fixture, not real terms or rules.",
        reason="Unchanged poll",
    )
    assert result[0] == original["revision"]
    assert service.get_payout_policy(
        "synthetic-book", store.clock(), store.clock()
    ).decision.allowed


def test_new_review_resolves_document_change(store, service, clock, enabled):
    clock.now += timedelta(seconds=1)
    _, sha256 = store.archive_document(
        "synthetic-book-rules",
        "payout:synthetic-book",
        "synthetic://changed",
        b"New approved synthetic rules",
        reason="Changed document",
    )
    old = enabled[2]
    new = type(old).model_validate(
        old.model_dump()
        | {
            "version": "fixture-v2",
            "reviewed_at": clock.now,
            "evidence": [{"document_id": "synthetic-book-rules", "sha256": sha256}],
        }
    )
    revision = store.records("payout", new.bookmaker)[-1]["revision"]
    store.save(
        PayoutSchedule(bookmaker=new.bookmaker, policies=(new,)),
        expected_revision=revision,
        reason="Reviewer accepted new document",
    )
    assert (
        service.get_payout_policy(new.bookmaker, clock.now, clock.now).policy.version
        == "fixture-v2"
    )


def test_backdated_effective_policy_not_available_before_it_was_known(store, service, clock):
    earlier = clock.now - timedelta(days=1)
    policy = payout_policy(store, effective_from=earlier)
    store.save(
        PayoutSchedule(bookmaker=policy.bookmaker, policies=(policy,)),
        expected_revision=0,
        reason="Newly learned historical rules",
    )
    assert (
        service.get_payout_policy(policy.bookmaker, earlier, earlier).decision.reason
        == "PAYOUT_POLICY_MISSING"
    )
    assert service.get_payout_policy(policy.bookmaker, earlier, clock.now).decision.allowed


def test_overlaps_rejected_and_adjacent_policy_boundaries_select_once(store, service, clock):
    first = payout_policy(store, effective_until=clock.now + timedelta(days=1))
    second = type(first).model_validate(
        first.model_dump()
        | {
            "version": "fixture-v2",
            "effective_from": first.effective_until,
            "effective_until": first.effective_until + timedelta(days=1),
        }
    )
    with pytest.raises(ValidationError, match="overlap"):
        overlap = type(second).model_validate(second.model_dump() | {"effective_from": clock.now})
        PayoutSchedule(bookmaker=first.bookmaker, policies=(first, overlap))
    store.save(
        PayoutSchedule(bookmaker=first.bookmaker, policies=(first, second)),
        expected_revision=0,
        reason="Adjacent intervals",
    )
    assert (
        service.get_payout_policy(first.bookmaker, second.effective_from, clock.now).policy.version
        == second.version
    )
    assert not service.get_payout_policy(
        first.bookmaker, second.effective_until, clock.now
    ).decision.allowed


@pytest.mark.parametrize("role", [Role.AGENT, Role.DASHBOARD, Role.OPERATOR])
def test_untrusted_roles_cannot_raise_limits_or_approve(store, role):
    policy = responsible_policy(store)
    store.principal = Principal(identity="untrusted", role=role)
    with pytest.raises(PermissionError):
        store.save(
            ResponsibleUseSchedule(account_scope="shadow", policies=(policy,)),
            expected_revision=0,
            reason="Attempt to change limits",
        )
    with pytest.raises(PermissionError):
        store.set_global_disable(False, reason="Attempt to enable recommendations")
    assert not store.records("responsible_use")


@pytest.mark.parametrize("role", [Role.AGENT, Role.DASHBOARD])
def test_untrusted_roles_cannot_poison_evidence(store, role):
    store.principal = Principal(identity="untrusted", role=role)
    with pytest.raises(PermissionError):
        store.archive_document(
            "poison", "payout:synthetic-book", "synthetic://bad", b"fake", reason="Attempt"
        )


def test_operator_can_stop_but_not_reenable(store):
    store.principal = Principal(identity="ops", role=Role.OPERATOR)
    store.set_global_disable(True, reason="Incident")
    assert store.global_disabled(store.clock())
    with pytest.raises(PermissionError):
        store.set_global_disable(False, reason="Unauthorized restart")


def test_missing_policies_are_no_bet(service, store):
    assert service.get_responsible_use_policy("shadow").decision.reason == "GLOBAL_DISABLE"
    store.set_global_disable(False, reason="Test missing policy")
    assert (
        service.get_responsible_use_policy("shadow").decision.reason
        == "RESPONSIBLE_USE_POLICY_MISSING"
    )
    assert (
        service.get_payout_policy("unknown", store.clock(), store.clock()).decision.recommendation
        == "NO_BET"
    )


def test_cooling_off_ends_at_exact_boundary(store, service, clock):
    policy = responsible_policy(store, cooling_off_until=clock.now + timedelta(hours=1))
    store.save(
        ResponsibleUseSchedule(account_scope="shadow", policies=(policy,)),
        expected_revision=0,
        reason="Cooling off",
    )
    store.set_global_disable(False, reason="Fixture")
    assert service.get_responsible_use_policy("shadow").decision.reason == "COOLING_OFF"
    clock.now += timedelta(hours=1)
    assert service.get_responsible_use_policy("shadow").decision.allowed


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"disable_recommendations": True}, "ACCOUNT_DISABLED"),
        ({"state": "SUSPENDED"}, "RESPONSIBLE_USE_POLICY_NOT_APPROVED"),
    ],
)
def test_account_stops_include_audit_revision(store, service, overrides, expected):
    policy = responsible_policy(store, **overrides)
    revision = store.save(
        ResponsibleUseSchedule(account_scope="shadow", policies=(policy,)),
        expected_revision=0,
        reason="Account stop fixture",
    )
    store.set_global_disable(False, reason="Synthetic fixture")
    result = service.get_responsible_use_policy("shadow").decision
    assert result.reason == expected
    assert result.revision == revision
    assert result.recommendation == "NO_BET"


def test_global_stop_overrides_previously_enabled_publication(store, service, enabled):
    store.set_global_disable(True, reason="Emergency stop")
    assert (
        service.publication_gate(
            [enabled[0].source_id], "synthetic-book", "shadow", Purpose.PRODUCTION
        ).reason
        == "GLOBAL_DISABLE"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"effective_from": datetime(2026, 9, 19)},
        {"reviewed_at": datetime(2026, 9, 19)},
        {"max_open_exposure": 0.1},
        {"max_bankroll_fraction": 0.1},
        {"max_open_exposure": "NaN"},
        {"max_open_exposure": "-1.00"},
        {"daily": {"stake": "1.001", "count": 1}},
        {"daily": {"stake": "1", "count": -1}},
        {"loss_chasing_allowed": True},
        {"reset_timezone": "UTC"},
        {"currency": "EUR"},
    ],
)
def test_invalid_policy_values_rejected(store, overrides):
    with pytest.raises(ValidationError):
        responsible_policy(store, **overrides)


def test_decimal_serialization_is_exact(store):
    policy = responsible_policy(store, max_open_exposure="0.10")
    assert json.loads(policy.model_dump_json())["max_open_exposure"] == "0.10"
    with pytest.raises(ValidationError):
        policy.max_open_exposure = "1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence": []},
        {"retention": None},
        {"quota": None},
        {"reviewer": None},
        {"commercial_use": "unknown"},
        {"allowed_purposes": ["redistribution"]},
        {"state": "PROTOTYPE_APPROVED", "allowed_purposes": ["production"]},
    ],
)
def test_unknown_approval_details_never_grant_permission(store, overrides):
    with pytest.raises(ValidationError):
        source_policy(store, **overrides)


def test_missing_evidence_and_spoofed_reviewer_cannot_be_approved(store):
    policy = source_policy(store)
    with pytest.raises(PermissionError):
        store.save(
            SourcePolicy.model_validate(policy.model_dump() | {"reviewer": "someone-else"}),
            expected_revision=0,
            reason="Spoof",
        )
    forged = SourcePolicy.model_validate(
        policy.model_dump()
        | {
            "evidence": [{"document_id": policy.evidence[0].document_id, "sha256": "0" * 64}],
        }
    )
    with pytest.raises(ValueError, match="DOCUMENT_REVIEW_REQUIRED"):
        store.save(forged, expected_revision=0, reason="Incorrect hash")


def test_review_cannot_predate_document_or_be_in_future(store, clock):
    for delta in [-1, 1]:
        policy = source_policy(store, reviewed_at=clock.now + timedelta(seconds=delta))
        with pytest.raises(ValueError, match="Review"):
            store.save(policy, expected_revision=0, reason="Invalid review time")


def test_policy_versions_immutable_and_stale_writes_rejected(store):
    policy = source_policy(store)
    revision = store.save(policy, expected_revision=0, reason="First")
    with pytest.raises(ConflictError, match="Stale"):
        store.save(policy, expected_revision=0, reason="Stale")
    changed = SourcePolicy.model_validate(policy.model_dump() | {"kill_switch": True})
    with pytest.raises(ConflictError, match="new version"):
        store.save(changed, expected_revision=revision, reason="No new version")
    for statement in [
        "DELETE FROM journal",
        "UPDATE journal SET reason='changed'",
        "DELETE FROM document_bytes",
        "UPDATE document_bytes SET content=X'00'",
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute(statement)


def test_reopened_store_sees_revocation_and_preserves_audit(store, clock, tmp_path):
    policy = source_policy(store)
    revision = store.save(policy, expected_revision=0, reason="Approved")
    other = GovernanceStore(tmp_path / "governance.sqlite3", store.principal, clock)
    try:
        service = GovernanceService(other)
        assert service.can_fetch(policy.source_id, Purpose.PRODUCTION).allowed
        replacement = SourcePolicy.model_validate(
            policy.model_dump() | {"version": "v2", "kill_switch": True}
        )
        store.save(replacement, expected_revision=revision, reason="Revoked")
        assert not service.can_fetch(policy.source_id, Purpose.PRODUCTION).allowed
        with pytest.raises(ConflictError):
            other.save(policy, expected_revision=revision, reason="Stale concurrent writer")
        assert len(other.export()["records"]) == 3
    finally:
        other.close()


def test_clock_cannot_backdate_knowledge(store, clock):
    store.set_global_disable(True, reason="First event")
    clock.now -= timedelta(seconds=1)
    with pytest.raises(ValueError, match="backdate"):
        store.set_global_disable(False, reason="Backdated restart")


@pytest.mark.parametrize(
    "instant,period,start,end",
    [
        (
            "2026-03-29T12:00:00+00:00",
            "daily",
            "2026-03-28T23:00:00+00:00",
            "2026-03-29T22:00:00+00:00",
        ),
        (
            "2026-10-25T12:00:00+00:00",
            "daily",
            "2026-10-24T22:00:00+00:00",
            "2026-10-25T23:00:00+00:00",
        ),
        (
            "2026-03-29T12:00:00+00:00",
            "weekly",
            "2026-03-22T23:00:00+00:00",
            "2026-03-29T22:00:00+00:00",
        ),
        (
            "2026-10-25T12:00:00+00:00",
            "weekly",
            "2026-10-18T22:00:00+00:00",
            "2026-10-25T23:00:00+00:00",
        ),
        (
            "2026-12-31T23:00:00+00:00",
            "monthly",
            "2026-12-31T23:00:00+00:00",
            "2027-01-31T23:00:00+00:00",
        ),
        (
            "2028-02-29T12:00:00+00:00",
            "monthly",
            "2028-01-31T23:00:00+00:00",
            "2028-02-29T23:00:00+00:00",
        ),
    ],
)
def test_warsaw_reset_boundaries(instant, period, start, end):
    assert reset_period(datetime.fromisoformat(instant), period) == (
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
    )


def test_fall_dst_repeated_hour_has_same_daily_period():
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert reset_period(first, "daily") == reset_period(first + timedelta(hours=1), "daily")


def test_unvalidated_pydantic_copy_cannot_bypass_save(store):
    policy = source_policy(store)
    with pytest.raises(ValidationError):
        store.save(policy.model_copy(update={"quota": None}), expected_revision=0, reason="Bypass")
