"""F15.7 backups for RTO 4 h / RPO 15 min: config, object mirror, journal snapshots."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.object_store import LocalObjectStore
from tennis_engine.monitoring.metrics import MetricsRegistry
from tennis_engine.operations.backups import (
    OBJECT_MARKER,
    RecoveryConfig,
    backup_objects,
    copy_missing,
    journal_snapshots,
    load_recovery_config,
    newest_base_backup,
    register_backup_collector,
    snapshot_journal,
)
from tennis_engine.operations.recovery import journal_fingerprint

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 30, 12, 7, tzinfo=UTC)


def test_the_agreed_objectives_are_loaded_and_the_schedule_meets_the_rpo():
    config = load_recovery_config(ROOT / "configs/operations/recovery-objectives.json")
    assert (config.status, str(config.accepted_on)) == ("ACCEPTED", "2026-09-30")
    assert (config.rto_seconds, config.rpo_seconds) == (4 * 3600, 15 * 60)
    assert config.objectives().rto_seconds == 14400
    assert config.design_findings() == ()
    assert max(config.worst_case_loss_seconds().values()) <= config.rpo_seconds
    slow = config.model_copy(
        update={
            "schedule": config.schedule.model_copy(update={"object_backup_interval_seconds": 1800})
        }
    )
    assert slow.design_findings() == ("SCHEDULE_EXCEEDS_RPO:objects",)
    with pytest.raises(ValueError):
        RecoveryConfig.model_validate(config.model_dump() | {"status": "PROPOSED"})


def test_object_backup_copies_new_keys_once_and_records_the_recovery_point(tmp_path):
    source = LocalObjectStore(tmp_path / "source")
    source.put("raw/a.json.gz", b"first")
    source.put("raw/b.json.gz", b"second")
    backup = tmp_path / "backup"
    first = backup_objects(source, backup, NOW)
    assert (first.listed, first.copied, first.problems) == (2, 2, ())
    assert (backup / OBJECT_MARKER).read_text() == str(int(NOW.timestamp()))
    again = backup_objects(source, backup, NOW + timedelta(minutes=5))
    assert (again.copied, again.problems) == (0, ())
    assert LocalObjectStore(backup).get("raw/b.json.gz") == b"second"
    # The marker is not an object, so a restore never copies it back.
    assert OBJECT_MARKER not in "".join(LocalObjectStore(backup).list_keys())

    # A restore copies the backup into an empty store.
    restored = LocalObjectStore(tmp_path / "restored")
    report = copy_missing(LocalObjectStore(backup), restored)
    assert (report.copied, restored.get("raw/a.json.gz")) == (2, b"first")


def test_object_backup_reports_failures_and_keeps_the_old_recovery_point(tmp_path):
    source = LocalObjectStore(tmp_path / "source")
    source.put("raw/a.json.gz", b"first")
    backup = tmp_path / "backup"
    backup_objects(source, backup, NOW)

    class Broken:
        def list_keys(self, prefix=""):
            return ("raw/a.json.gz", "raw/new.json.gz")

        def get(self, key):
            raise ConnectionError("object store down")

    report = backup_objects(Broken(), backup, NOW + timedelta(minutes=5))
    assert (report.copied, report.problems) == (0, ("OBJECT_COPY_FAILED:ConnectionError",))
    assert (backup / OBJECT_MARKER).read_text() == str(int(NOW.timestamp()))

    class Refusing:
        def exists(self, key):
            return False

        def put(self, key, content):
            raise FileExistsError("other bytes")

    conflict = copy_missing(source, Refusing())
    assert conflict.problems == ("OBJECT_CONFLICT",)


def test_journal_snapshots_are_consistent_idempotent_and_pruned(tmp_path):
    journal = tmp_path / "governance.sqlite3"
    store = GovernanceStore(journal, Principal(identity="fixture", role=Role.OPERATOR))
    store.set_source_stop("synthetic-book", True, reason="Synthetic stop")
    store.close()
    directory = tmp_path / "snapshots"
    first = snapshot_journal(journal, directory, NOW, keep=2)
    assert snapshot_journal(journal, directory, NOW, keep=2) == first
    assert journal_fingerprint(first) == journal_fingerprint(journal)
    for minutes in (5, 10):
        snapshot_journal(journal, directory, NOW + timedelta(minutes=minutes), keep=2)
    names = [path.name for path in journal_snapshots(directory)]
    assert names == ["governance-20260930T121200Z.sqlite3", "governance-20260930T121700Z.sqlite3"]
    with pytest.raises(ValueError):
        snapshot_journal(journal, directory, NOW, keep=0)


def test_backup_collector_reads_recovery_points_from_the_volumes(tmp_path):
    base = tmp_path / "postgres" / "base"
    (base / "20260929T000000Z").mkdir(parents=True)
    (base / "20260929T000000Z" / "COMPLETE").write_text("1790640000\n")
    (base / "20260930T000000Z.part").mkdir()
    assert newest_base_backup(base) == 1790640000
    objects = tmp_path / "objects"
    objects.mkdir()
    (objects / OBJECT_MARKER).write_text("1790726400")
    journal = tmp_path / "journal"
    registry = MetricsRegistry()
    register_backup_collector(
        registry,
        postgres_base=base,
        objects=objects,
        journal=journal,
        archiver=lambda: {
            "archived_count": 12.0,
            "failed_count": 0.0,
            "archived_at": 1790726000.0,
            "failed_at": None,
        },
    )
    text = registry.render()
    assert 'tennis_backup_last_success_timestamp_seconds{store="postgres_base"} 1790640000' in text
    assert 'tennis_backup_last_success_timestamp_seconds{store="objects"} 1790726400' in text
    # No journal snapshot yet: the sample is absent, so the alert treats it as missing.
    assert 'store="journal"' not in text
    assert 'tennis_wal_archive_timestamp_seconds{event="archived"} 1790726000' in text
    assert 'tennis_wal_archive_timestamp_seconds{event="failed"}' not in text
    assert 'tennis_wal_archive_segments{event="failed"} 0' in text


def test_a_failing_archiver_query_marks_the_collector_down(tmp_path):
    registry = MetricsRegistry()

    def broken():
        raise ConnectionError("database down")

    register_backup_collector(
        registry, postgres_base=None, objects=None, journal=None, archiver=broken
    )
    assert 'tennis_metrics_collector_up{collector="backups"} 0' in registry.render()


def test_postgres_backup_files_are_restricted():
    hba = (ROOT / "deploy/postgres/pg_hba.conf").read_text(encoding="utf-8")
    rules = [line.split() for line in hba.splitlines() if line and not line.startswith("#")]
    network = [rule for rule in rules if rule[0] == "host"]
    assert ["host", "replication", "tennis_backup", "all", "scram-sha-256"] in network
    assert all(rule[-1] == "scram-sha-256" for rule in network)
    assert not any(rule[1] == "replication" and rule[2] == "all" for rule in network)
    for name in ("archive-wal.sh", "base-backup.sh", "restore-pitr.sh"):
        script = (ROOT / "deploy/postgres" / name).read_bytes()
        assert b"\r\n" not in script and script.startswith(b"#!/bin/sh\n")
        assert b"set -eu" in script
