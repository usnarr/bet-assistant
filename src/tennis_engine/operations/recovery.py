"""F15.7 backup and restore verification: fingerprints, journal backup, reconciliation.

The database backup itself uses the PostgreSQL tools (`pg_dump`, `pg_restore`). This
module proves that a restored copy equals its source:

- A database fingerprint holds the Alembic revision, and a row count and an MD5 digest
  of the ordered rows of every table in the `tennis` schema.
- A journal fingerprint holds the F01 journal rows and checks each stored hash.
- Raw-object reconciliation checks that each archived raw content exists in the object
  store with the recorded SHA-256.
- Ledger reconciliation checks that each virtual ledger balances.

The owner agreed the recovery objectives on 2026-09-30: RTO 4 hours, RPO 15 minutes
(`configs/operations/recovery-objectives.json`). A report compares the measured restore
time and data-loss window with them and is `PASS` or `FAIL`. A caller that passes no
objectives still gets `BLOCKED`, so a missing objective can never pass.
"""

import gzip
import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field
from sqlalchemy import Engine, text

from tennis_engine.common.contracts import Contract, Timestamp
from tennis_engine.infrastructure.object_store import ImmutableObjectStore
from tennis_engine.ingestion.contracts import ArchiveState
from tennis_engine.ingestion.postgres import PostgresIngestionStore
from tennis_engine.settlement.ledger import VirtualLedgerService
from tennis_engine.settlement.postgres import PostgresLedgerStore

SCHEMA = "tennis"


class TableFingerprint(Contract):
    rows: int
    md5: str


class DatabaseFingerprint(Contract):
    revision: str | None
    tables: dict[str, TableFingerprint]
    taken_at: Timestamp


def database_fingerprint(engine: Engine, taken_at: datetime) -> DatabaseFingerprint:
    """A deterministic digest of every table. Read it inside one repeatable-read snapshot."""
    tables: dict[str, TableFingerprint] = {}
    with engine.connect() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        # Timestamps render in the session time zone; fix it so digests are portable.
        db.execute(text("SET LOCAL TimeZone = 'UTC'"))
        names = db.execute(
            text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema "
                "AND table_type = 'BASE TABLE' ORDER BY table_name"
            ),
            {"schema": SCHEMA},
        ).scalars()
        for name in list(names):
            quoted = '"' + name.replace('"', '""') + '"'
            row = db.execute(
                text(
                    f"SELECT count(*) AS rows, md5(coalesce(string_agg(t::text, E'\\n' "
                    f"ORDER BY t::text), '')) AS digest FROM {SCHEMA}.{quoted} t"
                )
            ).one()
            tables[name] = TableFingerprint(rows=row.rows, md5=row.digest)
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    return DatabaseFingerprint(revision=revision, tables=tables, taken_at=taken_at)


class JournalFingerprint(Contract):
    records: int
    last_revision: int
    chain_sha256: str
    problems: tuple[str, ...]


def backup_journal(source: Path, target: Path) -> None:
    """A consistent online copy of the F01 journal. The target must not exist."""
    if target.exists():
        raise FileExistsError("The backup target already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()


def journal_fingerprint(path: Path) -> JournalFingerprint:
    """Hash chain over the journal rows. Each row hash and document hash is checked."""
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    problems: list[str] = []
    chain = hashlib.sha256()
    count = last = 0
    try:
        rows = db.execute(
            "SELECT revision, kind, entity_key, recorded_at, actor, reason, payload, sha256 "
            "FROM journal ORDER BY revision"
        )
        for revision, kind, key, recorded_at, actor, reason, payload, sha in rows:
            count, last = count + 1, revision
            computed = hashlib.sha256(payload.encode()).hexdigest()
            if computed != sha:
                problems.append(f"JOURNAL_HASH_MISMATCH:{revision}")
            # The chain uses the computed payload hash, so any payload change alters it.
            line = json.dumps([revision, kind, key, recorded_at, actor, reason, sha, computed])
            chain.update(line.encode() + b"\n")
            if kind == "document":
                content = db.execute(
                    "SELECT content FROM document_bytes WHERE revision = ?", (revision,)
                ).fetchone()
                try:
                    expected = json.loads(payload)["content_sha256"]
                except (ValueError, KeyError, TypeError):
                    problems.append(f"DOCUMENT_METADATA_INVALID:{revision}")
                    continue
                if content is None or hashlib.sha256(content[0]).hexdigest() != expected:
                    problems.append(f"DOCUMENT_HASH_MISMATCH:{revision}")
    finally:
        db.close()
    return JournalFingerprint(
        records=count, last_revision=last, chain_sha256=chain.hexdigest(), problems=tuple(problems)
    )


class ObjectReconciliation(Contract):
    contents: int
    verified: int
    problems: tuple[str, ...]


def reconcile_raw_objects(engine: Engine, objects: ImmutableObjectStore) -> ObjectReconciliation:
    """Read-only check of archived raw content. It never changes a content state."""
    problems: list[str] = []
    contents = PostgresIngestionStore(engine).contents()
    verified = 0
    for content in contents:
        if content.state != ArchiveState.ARCHIVED:
            continue
        try:
            archived = objects.get(content.object_key)
        except Exception as error:  # noqa: BLE001 - every failure is a finding
            problems.append(f"OBJECT_MISSING:{content.content_id}:{type(error).__name__}")
            continue
        try:
            # F03 archives gzip-compressed bytes and records the hash of the raw body.
            body = gzip.decompress(archived)
        except (OSError, EOFError):
            problems.append(f"OBJECT_UNREADABLE:{content.content_id}")
            continue
        if hashlib.sha256(body).hexdigest() != content.body_sha256:
            problems.append(f"OBJECT_HASH_MISMATCH:{content.content_id}")
            continue
        verified += 1
    return ObjectReconciliation(contents=len(contents), verified=verified, problems=tuple(problems))


class LedgerCheck(Contract):
    ledgers: int
    unbalanced: tuple[str, ...]
    closing_balances: dict[str, str]


def reconcile_ledgers(engine: Engine, clock_now: datetime) -> LedgerCheck:
    with engine.connect() as db:
        ids = list(
            db.execute(
                text("SELECT ledger_id FROM tennis.settlement_ledger ORDER BY ledger_id")
            ).scalars()
        )

    class _Clock:
        def now(self) -> datetime:
            return clock_now

    service = VirtualLedgerService(PostgresLedgerStore(engine), _Clock())
    unbalanced, balances = [], {}
    for ledger_id in ids:
        report = service.reconcile(ledger_id)
        balances[ledger_id] = str(report.closing_balance.amount)
        if not report.balanced:
            unbalanced.append(ledger_id)
    return LedgerCheck(ledgers=len(ids), unbalanced=tuple(unbalanced), closing_balances=balances)


class Objectives(Contract):
    rto_seconds: int | None = Field(default=None, gt=0)
    rpo_seconds: int | None = Field(default=None, gt=0)


class RestoreReport(Contract):
    status: Literal["PASS", "FAIL", "BLOCKED"]
    integrity: Literal["PASS", "FAIL"]
    findings: tuple[str, ...]
    objectives: Objectives
    measured_restore_seconds: float | None
    measured_data_loss_seconds: float | None


def compare(
    expected: DatabaseFingerprint,
    restored: DatabaseFingerprint,
    *,
    journal: tuple[JournalFingerprint, JournalFingerprint] | None = None,
    extra_findings: Iterable[str] = (),
    objectives: Objectives | None = None,
    measured_restore_seconds: float | None = None,
    measured_data_loss_seconds: float | None = None,
) -> RestoreReport:
    objectives = objectives or Objectives()
    findings = list(extra_findings)
    if expected.revision != restored.revision:
        findings.append(f"REVISION:{expected.revision}!={restored.revision}")
    for name in sorted(set(expected.tables) | set(restored.tables)):
        left, right = expected.tables.get(name), restored.tables.get(name)
        if left != right:
            findings.append(f"TABLE:{name}")
    if journal is not None:
        source, copy = journal
        findings.extend(copy.problems)
        if (source.records, source.chain_sha256) != (copy.records, copy.chain_sha256):
            findings.append("JOURNAL:MISMATCH")
    integrity: Literal["PASS", "FAIL"] = "FAIL" if findings else "PASS"
    status: Literal["PASS", "FAIL", "BLOCKED"] = integrity
    if integrity == "PASS":
        if objectives.rto_seconds is None or objectives.rpo_seconds is None:
            status = "BLOCKED"
            findings.append("OBJECTIVES_UNSET")
        elif measured_restore_seconds is None or measured_data_loss_seconds is None:
            status = "FAIL"
            findings.append("OBJECTIVES_NOT_MEASURED")
        elif (
            measured_restore_seconds > objectives.rto_seconds
            or measured_data_loss_seconds > objectives.rpo_seconds
        ):
            status = "FAIL"
            findings.append("OBJECTIVES_NOT_MET")
    return RestoreReport(
        status=status,
        integrity=integrity,
        findings=tuple(findings),
        objectives=objectives,
        measured_restore_seconds=measured_restore_seconds,
        measured_data_loss_seconds=measured_data_loss_seconds,
    )
