"""OPS-02 point-in-time restore drill on a disposable Compose stack. Synthetic data only.

The drill uses the real F15.7 backup path: continuous WAL archiving, the base backup of
the `postgres-backup` service, and the object and journal backups of the scheduler.

1. Wait for a complete base backup. Seed synthetic rows and raw objects.
2. Load: write a numbered database mark every second and a raw-object mark every
   10 seconds while the scheduler takes its normal backups.
3. Stop the API and the scheduler. Fingerprint the `tennis` schema and copy the live
   journal. Keep writing database marks, then kill PostgreSQL with SIGKILL. The newest
   WAL segment is not archived, as after a real crash.
4. Restore (timed): a new PostgreSQL container with an empty volume replays the newest
   base backup and the WAL archive (`deploy/postgres/restore-pitr.sh`). The object
   backup goes into a new bucket and the newest journal snapshot is taken.
5. Verify the fingerprint, raw objects, ledgers and journal. The data-loss window of each
   store is the time between its last acknowledged write and its newest recovered write.
   The report compares both numbers with `configs/operations/recovery-objectives.json`.

Usage (Git Bash; the stack must be a disposable smoke stack, never a real one):

    uv run python scripts/ops02_pitr_drill.py --project f15bk-smoke \\
      --database-url postgresql+psycopg://tennis:local-only@127.0.0.1:55450/tennis \\
      --object-endpoint 127.0.0.1:59020 --restore-port 55451

The report prints no local path and no secret. Bring the stack down with its volumes
after the drill.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from minio import Minio
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from recovery_support import seed  # noqa: E402

from tennis_engine.infrastructure.database import build_engine  # noqa: E402
from tennis_engine.infrastructure.object_store import (  # noqa: E402
    LocalObjectStore,
    S3ObjectStore,
)
from tennis_engine.operations.backups import (  # noqa: E402
    JOURNAL_PREFIX,
    copy_missing,
    journal_snapshots,
    load_recovery_config,
)
from tennis_engine.operations.recovery import (  # noqa: E402
    compare,
    database_fingerprint,
    journal_fingerprint,
    reconcile_ledgers,
    reconcile_raw_objects,
)

MARK_PREFIX = "ops02-drill/mark-"
RESTORE_BUCKET = "ops02-restore"


def docker(*args: str, check: bool = True) -> str:
    done = subprocess.run(["docker", *args], check=check, capture_output=True, text=True)
    return done.stdout.strip()


def snapshot_time(path: Path) -> float:
    """The backup window in the snapshot name. The copy is at or after this time."""
    stamp = path.name.removeprefix(JOURNAL_PREFIX).removesuffix(".sqlite3")
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S%z").timestamp()


def wait_for_base_backup(container: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = docker("exec", container, "find", "/backup/base", "-name", "COMPLETE")
        if found:
            return
        time.sleep(2)
    raise RuntimeError("no complete base backup")


def wait_for_primary(url: str, timeout: float) -> Engine:
    """Poll until the restored server accepts connections and has left recovery."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        engine = build_engine(url)
        try:
            with engine.connect() as db:
                if not db.execute(text("SELECT pg_is_in_recovery()")).scalar_one():
                    return engine
        except Exception:  # noqa: BLE001 - the server is still starting
            pass
        engine.dispose()
        time.sleep(1)
    raise RuntimeError("the restored server did not become primary in time")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--object-endpoint", required=True)
    parser.add_argument("--object-access-key", default="local-development")
    parser.add_argument("--object-secret-key", default="change-me-before-use")
    parser.add_argument("--bucket", default="tennis-raw")
    parser.add_argument("--restore-port", type=int, required=True)
    parser.add_argument("--load-seconds", type=int, default=420)
    parser.add_argument("--settle-seconds", type=int, default=90)
    args = parser.parse_args()
    config = load_recovery_config(ROOT / "configs/operations/recovery-objectives.json")
    project = args.project
    postgres = f"{project}-postgres-1"
    scheduler = f"{project}-scheduler-1"
    image = docker("inspect", "--format", "{{.Config.Image}}", postgres)
    restore_container = f"{project}-ops02-restore"
    restore_volume = f"{project}_ops02-restore-data"
    restored_url = (
        make_url(args.database_url)
        .set(host="127.0.0.1", port=args.restore_port)
        .render_as_string(hide_password=False)
    )
    client = Minio(
        args.object_endpoint,
        access_key=args.object_access_key,
        secret_key=args.object_secret_key,
        secure=False,
    )
    workdir = Path(tempfile.mkdtemp(prefix="ops02-pitr-"))
    try:
        wait_for_base_backup(f"{project}-postgres-backup-1", 300)
        source = build_engine(args.database_url)
        objects = S3ObjectStore(client, args.bucket)
        seeded = seed(source, objects)
        with source.begin() as db:
            db.execute(text("CREATE SCHEMA ops02_drill"))
            db.execute(
                text("CREATE TABLE ops02_drill.marks (mark_id integer PRIMARY KEY, body text)")
            )

        # Acknowledged write time of each mark, measured after the commit returns.
        db_marks: dict[int, float] = {}
        object_marks: dict[int, float] = {}

        def write_mark(number: int) -> None:
            with source.begin() as db:
                db.execute(
                    text("INSERT INTO ops02_drill.marks VALUES (:n, :body)"),
                    {"n": number, "body": "synthetic drill mark"},
                )
            db_marks[number] = time.time()

        number = 0
        load_end = time.monotonic() + args.load_seconds
        while time.monotonic() < load_end:
            number += 1
            write_mark(number)
            if number % 10 == 1:
                objects.put(f"{MARK_PREFIX}{number:06d}", b"synthetic drill object")
                object_marks[number] = time.time()
            time.sleep(1)

        # The application stops; its tables must equal the fingerprint after the restore.
        docker("stop", "--time", "30", f"{project}-api-1", scheduler)
        now = datetime.now(UTC)
        expected = database_fingerprint(source, now)
        docker("cp", f"{scheduler}:/app/var/governance", str(workdir / "live"))
        live_journal = journal_fingerprint(workdir / "live" / "governance.sqlite3")
        settle_end = time.monotonic() + args.settle_seconds
        while time.monotonic() < settle_end:
            number += 1
            write_mark(number)
            time.sleep(1)
        source.dispose()
        docker("kill", "--signal", "KILL", postgres)
        disaster_at = time.time()

        started = time.perf_counter()
        docker("volume", "create", restore_volume)
        docker(
            "run",
            "--detach",
            "--name",
            restore_container,
            "--user",
            "70:70",
            "--publish",
            f"127.0.0.1:{args.restore_port}:5432",
            "--volume",
            f"{restore_volume}:/var/lib/postgresql/data",
            "--volume",
            f"{project}_postgres-backup:/backup:ro",
            "--volume",
            f"{ROOT / 'deploy' / 'postgres'}:/etc/tennis-postgres:ro",
            "--entrypoint",
            "sh",
            image,
            "/etc/tennis-postgres/restore-pitr.sh",
            "-c",
            "hba_file=/etc/tennis-postgres/pg_hba.conf",
        )
        restored = wait_for_primary(restored_url, config.rto_seconds)
        database_ready_seconds = time.perf_counter() - started
        docker("cp", f"{scheduler}:/backup/app/objects", str(workdir / "objects"))
        docker("cp", f"{scheduler}:/backup/app/journal", str(workdir / "journal"))
        client.make_bucket(RESTORE_BUCKET)
        restored_objects = S3ObjectStore(client, RESTORE_BUCKET)
        copied = copy_missing(LocalObjectStore(workdir / "objects"), restored_objects)
        snapshot = journal_snapshots(workdir / "journal")[-1]
        journals = (live_journal, journal_fingerprint(snapshot))
        actual = database_fingerprint(restored, now)
        raw = reconcile_raw_objects(restored, restored_objects)
        ledgers = reconcile_ledgers(restored, now)
        with restored.connect() as db:
            newest_db = db.execute(text("SELECT max(mark_id) FROM ops02_drill.marks")).scalar()
        restore_seconds = time.perf_counter() - started

        recovered_objects = [
            int(key.removeprefix(MARK_PREFIX)) for key in restored_objects.list_keys(MARK_PREFIX)
        ]
        losses = {
            "postgres": max(db_marks.values()) - db_marks[newest_db] if newest_db else None,
            "objects": (
                max(object_marks.values()) - object_marks[max(recovered_objects)]
                if recovered_objects
                else None
            ),
            "journal": (
                0.0
                if journals[0].chain_sha256 == journals[1].chain_sha256
                else disaster_at - snapshot_time(snapshot)
            ),
        }
        findings = list(raw.problems) + list(copied.problems)
        findings += [f"LEDGER:{item}" for item in ledgers.unbalanced]
        findings += [f"NO_RECOVERED_WRITE:{store}" for store, v in losses.items() if v is None]
        measured_loss = max(value for value in losses.values() if value is not None)
        report = compare(
            expected,
            actual,
            journal=journals,
            extra_findings=findings,
            objectives=config.objectives(),
            measured_restore_seconds=round(restore_seconds, 3),
            measured_data_loss_seconds=round(measured_loss, 3),
        )
        restored.dispose()
        print(
            json.dumps(
                {
                    "recovery_config": config.version,
                    "seeded": seeded,
                    "tables": len(expected.tables),
                    "revision": actual.revision,
                    "database_marks": {"written": len(db_marks), "recovered": newest_db},
                    "object_marks": {
                        "written": len(object_marks),
                        "recovered": len(recovered_objects),
                    },
                    "objects_restored": copied.copied,
                    "raw_objects": {"contents": raw.contents, "verified": raw.verified},
                    "ledgers": ledgers.model_dump(mode="json"),
                    "journal_records": journals[0].records,
                    "database_ready_seconds": round(database_ready_seconds, 3),
                    "data_loss_seconds": {
                        store: None if value is None else round(value, 3)
                        for store, value in losses.items()
                    },
                    "report": report.model_dump(mode="json"),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if report.status == "PASS" else 1
    finally:
        docker("rm", "--force", restore_container, check=False)
        docker("volume", "rm", restore_volume, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
