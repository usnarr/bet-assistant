"""OPS-02 restore drill on an isolated PostgreSQL container. Synthetic data only.

Steps: migrate and seed the source database, write a governance journal, fingerprint
both, back up the database with `pg_dump` and the journal with the SQLite backup API,
restore into a new database with `pg_restore`, then verify lineage, raw objects and the
ledger. The report prints no local path.

Usage (Git Bash; the database must be a disposable test database):

    TEST_DATABASE_URL=postgresql+psycopg://tennis:test-only@127.0.0.1:55442/tennis_test \\
      uv run python scripts/ops02_restore_drill.py --container f15-test-pg

Never point this script at a real database. It downgrades the source to base at the end.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from recovery_support import seed  # noqa: E402

from tennis_engine.governance.contracts import Principal, Role  # noqa: E402
from tennis_engine.governance.store import GovernanceStore  # noqa: E402
from tennis_engine.infrastructure.database import build_engine  # noqa: E402
from tennis_engine.infrastructure.object_store import LocalObjectStore  # noqa: E402
from tennis_engine.operations.recovery import (  # noqa: E402
    Objectives,
    backup_journal,
    compare,
    database_fingerprint,
    journal_fingerprint,
    reconcile_ledgers,
    reconcile_raw_objects,
)

RESTORE_DB = "tennis_ops02_restore"


def docker(container: str, *args: str) -> None:
    subprocess.run(["docker", "exec", container, *args], check=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--rto-seconds", type=int)
    parser.add_argument("--rpo-seconds", type=int)
    args = parser.parse_args()
    source_url = os.environ["TEST_DATABASE_URL"]
    url = make_url(source_url)
    user, database = url.username or "", url.database or ""
    restored_url = url.set(database=RESTORE_DB).render_as_string(hide_password=False)
    os.environ["TENNIS_DATABASE_URL"] = source_url
    config = Config(str(ROOT / "alembic.ini"))
    workdir = Path(tempfile.mkdtemp(prefix="ops02-"))
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        source = build_engine(source_url)
        objects_root = workdir / "objects"
        seeded = seed(source, LocalObjectStore(objects_root))
        journal = workdir / "governance.sqlite3"
        reviewer = Principal(identity="ops02-drill", role=Role.POLICY_REVIEWER)
        store = GovernanceStore(journal, reviewer)
        store.set_global_disable(True, reason="OPS-02 drill: stop stays on")
        store.set_source_stop("synthetic-book", True, reason="OPS-02 drill source stop")
        store.close()
        now = datetime.now(UTC)
        expected = database_fingerprint(source, now)
        last_write = time.time()

        started = time.perf_counter()
        docker(
            args.container, "pg_dump", "-U", user, "-d", database, "-Fc", "-f", "/tmp/ops02.dump"
        )
        backup_journal(journal, workdir / "backup" / "governance.sqlite3")
        shutil.copytree(objects_root, workdir / "backup" / "objects")
        backup_seconds = time.perf_counter() - started
        data_loss_seconds = max(0.0, time.time() - last_write - backup_seconds)

        started = time.perf_counter()
        docker(args.container, "dropdb", "-U", user, "--if-exists", RESTORE_DB)
        docker(args.container, "createdb", "-U", user, RESTORE_DB)
        docker(args.container, "pg_restore", "-U", user, "-d", RESTORE_DB, "/tmp/ops02.dump")
        restored = build_engine(restored_url)
        actual = database_fingerprint(restored, now)
        journals = (
            journal_fingerprint(journal),
            journal_fingerprint(workdir / "backup" / "governance.sqlite3"),
        )
        raw = reconcile_raw_objects(restored, LocalObjectStore(workdir / "backup" / "objects"))
        ledgers = reconcile_ledgers(restored, now)
        restore_seconds = time.perf_counter() - started
        findings = list(raw.problems) + [f"LEDGER:{item}" for item in ledgers.unbalanced]
        report = compare(
            expected,
            actual,
            journal=journals,
            extra_findings=findings,
            objectives=Objectives(rto_seconds=args.rto_seconds, rpo_seconds=args.rpo_seconds),
            measured_restore_seconds=round(restore_seconds, 3),
            measured_data_loss_seconds=round(data_loss_seconds, 3),
        )
        restored.dispose()
        source.dispose()
        print(
            json.dumps(
                {
                    "seeded": seeded,
                    "tables": len(expected.tables),
                    "rows": sum(item.rows for item in expected.tables.values()),
                    "revision": actual.revision,
                    "journal_records": journals[0].records,
                    "raw_objects": {"contents": raw.contents, "verified": raw.verified},
                    "ledgers": ledgers.model_dump(mode="json"),
                    "backup_seconds": round(backup_seconds, 3),
                    "report": report.model_dump(mode="json"),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if report.integrity == "PASS" else 1
    finally:
        docker(args.container, "dropdb", "-U", user, "--if-exists", RESTORE_DB)
        docker(args.container, "rm", "-f", "/tmp/ops02.dump")
        command.downgrade(config, "base")
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
