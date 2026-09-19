"""Local, append-only policy journal with transactional, optimistic updates.

This is a trusted local service, not an authentication server. Its principal must
come from the host's authorization layer. Agents/viewers receive only read APIs.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from .contracts import (
    EvidenceRef,
    Lifecycle,
    PayoutPolicy,
    PayoutSchedule,
    Principal,
    ResponsibleUsePolicy,
    ResponsibleUseSchedule,
    Role,
    SourcePolicy,
    utc,
)

PolicyDocument = SourcePolicy | PayoutSchedule | ResponsibleUseSchedule


def system_clock() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class ConflictError(ValueError):
    """A stale revision or incompatible policy change requires a fresh review."""


class GovernanceStore:
    def __init__(
        self,
        path: str | Path,
        principal: Principal,
        clock: Callable[[], datetime] = system_clock,
    ) -> None:
        self.principal = principal
        self.clock = clock
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("Unsupported governance schema")
        self.db.executescript(files(__package__).joinpath("001_governance.sql").read_text())
        self.db.execute("PRAGMA user_version=1")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def require_role(self, *roles: Role) -> None:
        if self.principal.role not in roles:
            raise PermissionError("This operation requires a trusted policy reviewer/operator")

    def latest(self, kind: str, key: str, known_at: datetime) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self.db.execute(
                "SELECT * FROM journal WHERE kind=? AND entity_key=? AND recorded_at<=? "
                "ORDER BY revision DESC LIMIT 1",
                (kind, key, timestamp(known_at)),
            ).fetchone(),
        )

    def records(self, kind: str, key: str | None = None) -> list[sqlite3.Row]:
        if key is None:
            return list(
                self.db.execute("SELECT * FROM journal WHERE kind=? ORDER BY revision", (kind,))
            )
        return list(
            self.db.execute(
                "SELECT * FROM journal WHERE kind=? AND entity_key=? ORDER BY revision", (kind, key)
            )
        )

    def _append(self, kind: str, key: str, payload: dict[str, Any], reason: str) -> int:
        if not reason.strip():
            raise ValueError("An audit reason is required")
        now = timestamp(self.clock())
        last = self.db.execute(
            "SELECT recorded_at FROM journal ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if last and now < last[0]:
            raise ValueError("Clock moved backwards; refusing to backdate knowledge")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        cursor = self.db.execute(
            "INSERT INTO journal(kind,entity_key,recorded_at,actor,reason,payload,sha256) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                kind,
                key,
                now,
                self.principal.identity,
                reason.strip(),
                encoded,
                digest(encoded.encode()),
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def save(self, document: PolicyDocument, *, expected_revision: int, reason: str) -> int:
        self.require_role(Role.POLICY_REVIEWER)
        # Revalidate even instances constructed with Pydantic's unchecked model_copy/construct.
        document = type(document).model_validate_json(document.model_dump_json())
        policies: tuple[SourcePolicy | PayoutPolicy | ResponsibleUsePolicy, ...]
        if isinstance(document, SourcePolicy):
            kind, key, policies = "source", document.source_id, (document,)
        elif isinstance(document, PayoutSchedule):
            kind, key, policies = "payout", document.bookmaker, document.policies
        else:
            kind, key, policies = "responsible_use", document.account_scope, document.policies
        with self.transaction():
            latest = self.latest(kind, key, self.clock())
            if (latest["revision"] if latest else 0) != expected_revision:
                raise ConflictError("Stale policy revision; read and review the current version")
            past_versions: dict[str, dict[str, Any]] = {}
            for row in self.records(kind, key):
                old = json.loads(row["payload"])
                for policy in [old] if kind == "source" else old["policies"]:
                    past_versions[policy["version"]] = policy
            for policy in policies:
                body = policy.model_dump(mode="json")
                previous = past_versions.get(policy.version)
                if previous is not None and previous != body:
                    raise ConflictError("A changed policy needs a new version identifier")
                approved = policy.state in (
                    Lifecycle.PROTOTYPE_APPROVED,
                    Lifecycle.PRODUCTION_APPROVED,
                    "APPROVED",
                )
                if approved and previous is None:
                    if policy.reviewer != self.principal.identity:
                        raise PermissionError("Reviewer must match the authenticated principal")
                    if policy.reviewed_at is None or policy.reviewed_at > utc(self.clock()):
                        raise ValueError("Review cannot be in the future")
                    if policy.review_due_at is None or policy.review_due_at <= utc(self.clock()):
                        raise ValueError("Cannot approve an already expired review")
                    problem = self.evidence_problem(f"{kind}:{key}", policy.evidence, self.clock())
                    if problem:
                        raise ValueError(problem)
                    # No document can have been reviewed before it became available here.
                    for ref in policy.evidence:
                        observation = self.latest("document", ref.document_id, self.clock())
                        assert observation is not None
                        if datetime.fromisoformat(observation["recorded_at"]) > policy.reviewed_at:
                            raise ValueError("Review predates archived evidence")
            return self._append(kind, key, document.model_dump(mode="json"), reason)

    def archive_document(
        self, document_id: str, scope: str, reference: str, content: bytes, *, reason: str
    ) -> tuple[int, str]:
        self.require_role(Role.OPERATOR, Role.POLICY_REVIEWER)
        sha256 = digest(content)
        EvidenceRef(document_id=document_id, sha256=sha256)
        if (
            not content
            or not reference.strip()
            or not scope.startswith(("source:", "payout:", "responsible_use:"))
        ):
            raise ValueError("Nonempty content, reference and policy scope are required")
        with self.transaction():
            previous = self.latest("document", document_id, self.clock())
            if previous:
                old = json.loads(previous["payload"])
                if old["scope"] != scope:
                    raise ConflictError("A document cannot move between policy scopes")
                if old["content_sha256"] == sha256:
                    return previous["revision"], sha256
            revision = self._append(
                "document",
                document_id,
                {"scope": scope, "reference": reference, "content_sha256": sha256},
                reason,
            )
            self.db.execute("INSERT INTO document_bytes VALUES (?,?)", (revision, content))
            return revision, sha256

    def evidence_problem(
        self, scope: str, refs: tuple[EvidenceRef, ...], known_at: datetime
    ) -> str | None:
        latest: dict[str, sqlite3.Row] = {}
        for row in self.records("document"):
            if row["recorded_at"] <= timestamp(known_at):
                latest[row["entity_key"]] = row
        relevant = {
            key: row for key, row in latest.items() if json.loads(row["payload"])["scope"] == scope
        }
        expected = {item.document_id: item.sha256 for item in refs}
        if not expected or not relevant:
            return "EVIDENCE_MISSING"
        if set(expected) != set(relevant):
            return "DOCUMENT_REVIEW_REQUIRED"
        for key, row in relevant.items():
            metadata = json.loads(row["payload"])
            if expected[key] != metadata["content_sha256"]:
                return "DOCUMENT_REVIEW_REQUIRED"
            raw = self.db.execute(
                "SELECT content FROM document_bytes WHERE revision=?", (row["revision"],)
            ).fetchone()
            if raw is None or digest(raw[0]) != expected[key]:
                return "EVIDENCE_INTEGRITY_FAILURE"
        return None

    def set_global_disable(self, disabled: bool, *, reason: str) -> int:
        self.require_role(Role.OPERATOR, Role.POLICY_REVIEWER)
        if not disabled:
            self.require_role(Role.POLICY_REVIEWER)
        with self.transaction():
            return self._append("global_disable", "all", {"disabled": disabled}, reason)

    def global_disabled(self, now: datetime) -> bool:
        row = self.latest("global_disable", "all", now)
        return True if row is None else bool(json.loads(row["payload"])["disabled"])

    def export(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "records": [
                {**dict(row), "payload": json.loads(row["payload"])}
                for row in self.db.execute("SELECT * FROM journal ORDER BY revision")
            ],
        }
