import json
import sqlite3
from datetime import timedelta

import pytest
from conftest import source_policy

from tennis_engine.governance.contracts import Principal, Purpose, Role
from tennis_engine.governance.retention import RetentionService
from tennis_engine.governance.service import PermissionDenied
from tennis_engine.governance.store import ConflictError


def test_expiry_is_exact_idempotent_and_removes_bytes_atomically(store, clock, enabled):
    retention = RetentionService(store)
    revision = retention.archive_raw("sample", "synthetic-sports", Purpose.PRODUCTION, b"sample")
    assert (
        retention.archive_raw("sample", "synthetic-sports", Purpose.PRODUCTION, b"sample")
        == revision
    )
    assert retention.replay_available("sample")
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("DELETE FROM raw_bytes")
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE raw_bytes SET content=X'00'")
    with pytest.raises(ConflictError):
        retention.archive_raw("sample", "synthetic-sports", Purpose.PRODUCTION, b"changed")
    clock.now += timedelta(days=7)
    assert not retention.replay_available("sample")
    assert retention.expire_due() == 1
    assert retention.expire_due() == 0
    assert not store.db.execute("SELECT * FROM raw_bytes").fetchall()
    tombstone = json.loads(store.records("tombstone")[0]["payload"])
    assert tombstone["raw_revision"] == revision
    assert tombstone["cause"] == "retention_expired"
    assert tombstone["replay_available"] is False
    assert (
        retention.delete_raw("sample", reason="Retry") == store.records("tombstone")[0]["revision"]
    )
    with pytest.raises(ConflictError, match="resurrected"):
        retention.archive_raw("sample", "synthetic-sports", Purpose.PRODUCTION, b"sample")


def test_early_deletion_needs_reviewed_mandate(store, enabled):
    retention = RetentionService(store)
    retention.archive_raw("sample", "synthetic-sports", Purpose.PRODUCTION, b"sample")
    with pytest.raises(ValueError, match="mandate"):
        retention.delete_raw("sample", reason="Too early")
    assert retention.replay_available("sample")
    retention.delete_raw(
        "sample", reason="License revoked", mandate_reference="synthetic://mandate"
    )
    assert not retention.replay_available("sample")
    assert json.loads(store.records("tombstone")[0]["payload"])["cause"] == "licensed_deletion"


def test_failed_delete_rolls_back_tombstone(store, clock, enabled):
    retention = RetentionService(store)
    retention.archive_raw("sample", "synthetic-sports", Purpose.PRODUCTION, b"sample")
    clock.now += timedelta(days=7)
    store.db.execute(
        "CREATE TRIGGER simulate_disk_failure BEFORE DELETE ON raw_bytes "
        "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        retention.expire_due()
    assert not store.records("tombstone")
    assert store.db.execute("SELECT content FROM raw_bytes").fetchone()[0] == b"sample"
    store.db.execute("DROP TRIGGER simulate_disk_failure")
    assert retention.expire_due() == 1


def test_retention_is_source_specific(store, clock, enabled):
    second = source_policy(
        store, source_id="one-day", retention={"raw_days": 1, "basis": "Fixture"}
    )
    store.save(second, expected_revision=0, reason="One day license")
    retention = RetentionService(store)
    retention.archive_raw("short", "one-day", Purpose.PRODUCTION, b"short")
    retention.archive_raw("long", "synthetic-sports", Purpose.PRODUCTION, b"long")
    clock.now += timedelta(days=1)
    assert retention.expire_due() == 1
    assert retention.replay_available("long")
    assert not retention.replay_available("short")


def test_unapproved_source_cannot_be_archived(store):
    with pytest.raises(PermissionDenied):
        RetentionService(store).archive_raw("bad", "unknown", Purpose.PROTOTYPE, b"bad")
    assert not store.records("raw")


@pytest.mark.parametrize("role", [Role.AGENT, Role.DASHBOARD, Role.OPERATOR])
def test_roles_cannot_delete_before_expiry(store, enabled, role):
    retention = RetentionService(store)
    retention.archive_raw("sample", "synthetic-sports", Purpose.PRODUCTION, b"sample")
    store.principal = Principal(identity="untrusted", role=role)
    with pytest.raises(PermissionError):
        retention.delete_raw("sample", reason="Early deletion", mandate_reference="fake")
    assert retention.replay_available("sample")
