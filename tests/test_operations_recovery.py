"""F15.5/F15.7 journal backup, restore comparison, incident bundles and security checks."""

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import source_policy
from serving_support import BOOK_SOURCE, READ_AT, bet, copy, no_bet, no_quote, stored, watch

from tennis_engine.governance.contracts import Principal, Purpose, Role
from tennis_engine.governance.service import GovernanceService
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.operations.incidents import Category, affected, open_incident, verify_bundle
from tennis_engine.operations.recovery import (
    DatabaseFingerprint,
    Objectives,
    TableFingerprint,
    backup_journal,
    compare,
    journal_fingerprint,
)
from tennis_engine.serving.store import InMemoryDecisionStore

SOURCE_ROOT = Path(__file__).parents[1] / "src"


def fingerprint(**tables):
    return DatabaseFingerprint(
        revision="0011_operations",
        tables={name: TableFingerprint(rows=rows, md5="0" * 32) for name, rows in tables.items()},
        taken_at=READ_AT,
    )


# Journal backup ------------------------------------------------------------------------


def test_journal_backup_is_identical_and_tampering_is_detected(store, enabled, tmp_path):
    source = tmp_path / "governance.sqlite3"
    backup = tmp_path / "backup" / "governance.sqlite3"
    backup_journal(source, backup)
    original, copied = journal_fingerprint(source), journal_fingerprint(backup)
    assert original == copied and original.problems == () and original.records > 3
    with pytest.raises(FileExistsError):
        backup_journal(source, backup)

    # Tamper with the copy: drop the append-only trigger, then edit a payload.
    db = sqlite3.connect(backup)
    db.execute("DROP TRIGGER journal_no_update")
    last = db.execute("SELECT max(revision) FROM journal").fetchone()[0]
    db.execute("UPDATE journal SET payload = ? WHERE revision = ?", ('{"disabled":true}', last))
    db.commit()
    db.close()
    tampered = journal_fingerprint(backup)
    assert f"JOURNAL_HASH_MISMATCH:{last}" in tampered.problems
    report = compare(fingerprint(a=1), fingerprint(a=1), journal=(original, tampered))
    assert report.status == "FAIL" and "JOURNAL:MISMATCH" in report.findings


def test_restore_comparison_needs_agreed_objectives():
    same = compare(fingerprint(a=1, b=2), fingerprint(a=1, b=2))
    assert same.integrity == "PASS" and same.status == "BLOCKED"
    assert "OBJECTIVES_UNSET" in same.findings
    objectives = Objectives(rto_seconds=3600, rpo_seconds=900)
    met = compare(
        fingerprint(a=1),
        fingerprint(a=1),
        objectives=objectives,
        measured_restore_seconds=12.5,
        measured_data_loss_seconds=0,
    )
    assert met.status == "PASS"
    late = compare(
        fingerprint(a=1),
        fingerprint(a=1),
        objectives=objectives,
        measured_restore_seconds=7200,
        measured_data_loss_seconds=0,
    )
    assert late.status == "FAIL" and "OBJECTIVES_NOT_MET" in late.findings
    unmeasured = compare(fingerprint(a=1), fingerprint(a=1), objectives=objectives)
    assert unmeasured.status == "FAIL" and "OBJECTIVES_NOT_MEASURED" in unmeasured.findings
    changed = compare(fingerprint(a=1, b=2), fingerprint(a=1, b=3))
    assert changed.status == "FAIL" and changed.findings == ("TABLE:b",)


# Incidents -------------------------------------------------------------------------------


@pytest.fixture
def operator(tmp_path, store, enabled, clock):
    store.save(source_policy(store, BOOK_SOURCE), expected_revision=0, reason="Synthetic book")
    principal = Principal(identity="fixture-operator", role=Role.OPERATOR)
    instance = GovernanceStore(tmp_path / "governance.sqlite3", principal, clock)
    yield instance
    instance.close()


def decisions():
    store = InMemoryDecisionStore()
    items = [stored(bet()), stored(watch()), stored(no_bet())]
    other = copy(no_quote(), "other-source")
    items.append(
        stored(
            other,
            source_ids=("synthetic-sports",),
        )
    )
    for item in items:
        store.add(item)
    return store, items


def test_incident_stops_first_and_preserves_evidence(operator, tmp_path, clock):
    store, items = decisions()
    decided = items[0].record.decided_at
    record, directory = open_incident(
        root=tmp_path / "artifacts",
        store=store,
        governance=operator,
        category=Category.PARSER_DRIFT,
        severity="CRITICAL",
        summary="Synthetic parser drift drill",
        opened_at=clock(),
        window_start=decided - timedelta(hours=1),
        window_end=decided + timedelta(hours=1),
        source_ids=[BOOK_SOURCE],
        stop_sources=True,
    )
    expected = sorted(
        str(item.record.decision_id) for item in items if BOOK_SOURCE in item.context.source_ids
    )
    assert sorted(map(str, record.affected_recommendation_ids)) == expected
    assert len(expected) == 3
    assert [(s.scope, s.target, s.outcome) for s in record.stops] == [
        ("source", BOOK_SOURCE, "APPLIED")
    ]
    denied = GovernanceService(operator).can_fetch(BOOK_SOURCE, Purpose.PROTOTYPE, clock())
    assert denied.reason.value == "SOURCE_STOPPED"
    assert verify_bundle(directory) == ()
    lines = (directory / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(line)["record"]["decision_id"] for line in lines} == set(expected)
    governance = json.loads((directory / "governance.json").read_text(encoding="utf-8"))
    assert any(row["kind"] == "source_stop" for row in governance["records"])
    # Stored decisions are unchanged.
    assert all(store.get(item.record.decision_id) == item for item in items)
    # The bundle is immutable and tampering is detected.
    with pytest.raises(FileExistsError):
        open_incident(
            root=tmp_path / "artifacts",
            store=store,
            governance=operator,
            category=Category.PARSER_DRIFT,
            severity="CRITICAL",
            summary="Synthetic parser drift drill",
            opened_at=clock(),
            window_start=decided - timedelta(hours=1),
            window_end=decided + timedelta(hours=1),
            source_ids=[BOOK_SOURCE],
            stop_sources=True,
        )
    (directory / "decisions.jsonl").write_text("", encoding="utf-8")
    assert verify_bundle(directory) == ("HASH_MISMATCH:decisions.jsonl",)


def test_affected_decisions_respect_the_window_and_scope():
    store, items = decisions()
    decided = items[0].record.decided_at
    everything = affected(
        store, window_start=decided - timedelta(hours=1), window_end=decided + timedelta(hours=1)
    )
    assert len(everything) == len(items)
    assert (
        affected(
            store,
            window_start=decided + timedelta(hours=1),
            window_end=decided + timedelta(hours=2),
        )
        == ()
    )
    by_book = affected(
        store,
        window_start=decided - timedelta(hours=1),
        window_end=decided + timedelta(hours=1),
        bookmakers=["synthetic-book"],
    )
    assert len(by_book) >= 3
    with pytest.raises(ValueError):
        affected(store, window_start=decided, window_end=decided)


# Security hardening (F15.5) --------------------------------------------------------------

UNSAFE = {
    "pickle": re.compile(r"^\s*(import|from)\s+(c?pickle|dill|joblib|shelve|marshal)\b", re.M),
    "eval": re.compile(r"(?<![\w.])(eval|exec)\("),
    "yaml.load": re.compile(r"yaml\.load\("),
    "shell=True": re.compile(r"shell\s*=\s*True"),
    "torch.load": re.compile(r"torch\.load\("),
}


def test_source_has_no_unsafe_deserialization_or_shell_execution():
    findings = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        content = path.read_text(encoding="utf-8")
        for name, pattern in UNSAFE.items():
            if pattern.search(content):
                findings.append(f"{path.relative_to(SOURCE_ROOT)}:{name}")
    assert findings == []


def test_model_artifacts_load_only_with_a_matching_hash():
    from tennis_engine.models.tabular import booster

    source = Path(booster.__file__).read_text(encoding="utf-8")
    load = source[source.index("def load_booster") :]
    load = load[: load.index("\ndef ")]
    # The hash check precedes the only load call, and the format is JSON, not pickle.
    assert load.index("booster_sha256") < load.index("load_model")


def test_timestamps_in_reports_are_aware():
    assert fingerprint().taken_at.tzinfo is not None
    assert datetime.now(UTC).tzinfo is not None


def test_ops_cli_backs_up_the_journal_and_verifies_bundles(operator, tmp_path, clock, capsys):
    from tennis_engine.operations import cli as ops_cli

    target = tmp_path / "copy" / "governance.sqlite3"
    source = tmp_path / "governance.sqlite3"
    args = ["backup-journal", "--source", str(source), "--target", str(target)]
    assert ops_cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []
    assert ops_cli.main(args) == 2  # the target exists; a backup never overwrites
    capsys.readouterr()
    store, items = decisions()
    decided = items[0].record.decided_at
    _, directory = open_incident(
        root=tmp_path / "artifacts",
        store=store,
        governance=operator,
        category=Category.STALE_PUBLICATION,
        severity="CRITICAL",
        summary="Synthetic stale publication drill",
        opened_at=clock(),
        window_start=decided - timedelta(hours=1),
        window_end=decided + timedelta(hours=1),
        global_stop=True,
    )
    assert ops_cli.main(["verify-incident", str(directory)]) == 0
    assert operator.global_disabled(clock()) is True
