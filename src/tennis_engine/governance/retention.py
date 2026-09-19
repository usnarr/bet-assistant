"""Minimal local raw-object retention adapter for the F01 contract.

F03 will supply ingestion and object-storage adapters. Deletion and its tombstone
are one SQLite transaction here, so replay never silently loses an object.
"""

import json
from datetime import datetime, timedelta

from .contracts import Purpose, Role, SourcePolicy, utc
from .service import GovernanceService, PermissionDenied
from .store import ConflictError, GovernanceStore, digest, timestamp


class RetentionService:
    def __init__(self, store: GovernanceStore):
        self.store = store

    def archive_raw(self, object_id: str, source_id: str, purpose: Purpose, content: bytes) -> int:
        self.store.require_role(Role.OPERATOR, Role.POLICY_REVIEWER)
        if not object_id.strip() or not content:
            raise ValueError("Object ID and content are required")
        with self.store.transaction():
            permission = GovernanceService(self.store).can_fetch(source_id, purpose)
            if not permission.allowed:
                raise PermissionDenied(permission)
            previous = self.store.latest("raw", object_id, self.store.clock())
            if previous:
                old = json.loads(previous["payload"])
                if old["source_id"] != source_id or old["content_sha256"] != digest(content):
                    raise ConflictError("Raw object IDs cannot be overwritten")
                if not self.replay_available(object_id):
                    raise ConflictError("Expired/deleted objects cannot be resurrected")
                return int(previous["revision"])
            source = self.store.latest("source", source_id, self.store.clock())
            assert source is not None
            policy = SourcePolicy.model_validate_json(source["payload"])
            assert policy.retention is not None
            expires_at = utc(self.store.clock()) + timedelta(days=policy.retention.raw_days)
            revision = self.store._append(
                "raw",
                object_id,
                {
                    "source_id": source_id,
                    "source_version": policy.version,
                    "source_revision": source["revision"],
                    "content_sha256": digest(content),
                    "expires_at": timestamp(expires_at),
                    "retention": policy.retention.model_dump(),
                },
                "Archive under the source-specific retention policy",
            )
            self.store.db.execute("INSERT INTO raw_bytes VALUES (?,?)", (object_id, content))
            return revision

    def delete_raw(
        self, object_id: str, *, reason: str, mandate_reference: str | None = None
    ) -> int:
        self.store.require_role(Role.OPERATOR, Role.POLICY_REVIEWER)
        with self.store.transaction():
            prior = self.store.latest("tombstone", object_id, self.store.clock())
            if prior:
                return int(prior["revision"])  # Retry is idempotent.
            record = self.store.latest("raw", object_id, self.store.clock())
            if record is None:
                raise ValueError("Unknown raw object")
            metadata = json.loads(record["payload"])
            expired = utc(self.store.clock()) >= datetime.fromisoformat(metadata["expires_at"])
            if not expired:
                self.store.require_role(Role.POLICY_REVIEWER)
                if not mandate_reference or not mandate_reference.strip():
                    raise ValueError("Early licensed deletion requires a mandate reference")
            revision = self.store._append(
                "tombstone",
                object_id,
                {
                    "raw_revision": record["revision"],
                    "content_sha256": metadata["content_sha256"],
                    "cause": "retention_expired" if expired else "licensed_deletion",
                    "mandate_reference": mandate_reference,
                    "replay_available": False,
                },
                reason,
            )
            self.store.db.execute("DELETE FROM raw_bytes WHERE object_id=?", (object_id,))
            return revision

    def replay_available(self, object_id: str) -> bool:
        now = utc(self.store.clock())
        record = self.store.latest("raw", object_id, now)
        if record is None or self.store.latest("tombstone", object_id, now):
            return False
        metadata = json.loads(record["payload"])
        if now >= datetime.fromisoformat(metadata["expires_at"]):
            return False  # Expiry blocks reads even before a cleanup job runs.
        raw = self.store.db.execute(
            "SELECT content FROM raw_bytes WHERE object_id=?", (object_id,)
        ).fetchone()
        return raw is not None and digest(raw[0]) == metadata["content_sha256"]

    def expire_due(self) -> int:
        self.store.require_role(Role.OPERATOR, Role.POLICY_REVIEWER)
        count = 0
        for row in self.store.records("raw"):
            if self.store.latest("tombstone", row["entity_key"], self.store.clock()):
                continue
            if utc(self.store.clock()) >= datetime.fromisoformat(
                json.loads(row["payload"])["expires_at"]
            ):
                self.delete_raw(row["entity_key"], reason="Source retention expired")
                count += 1
        return count
