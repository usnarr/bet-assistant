"""PostgreSQL persistence for immutable F03 ingestion history."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from .contracts import (
    ArchiveState,
    ContentRecord,
    DeadLetterStatus,
    FetchResult,
    ObservationRecord,
    ParseResult,
    ReplayRequest,
    ReplayResult,
    ReplaySelection,
)
from .store import (
    CommandRecord,
    DeadLetterRevision,
    DerivedRecord,
    IngestionStore,
    OrphanObject,
    SourceRuntimeEvent,
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _content(row: Any) -> ContentRecord:
    data = row._mapping
    return ContentRecord(
        content_id=data["content_id"],
        source_id=data["source_id"],
        body_sha256=data["content_sha256"],
        object_key=data["object_key"],
        content_type=data["content_type"],
        size_bytes=data["size_bytes"],
        state=data["archive_state"],
        created_at=data["created_at"],
    )


class PostgresIngestionStore(IngestionStore):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def start_command(
        self,
        *,
        idempotency_key: str,
        source_id: str,
        logical_resource_id: str,
        observation_window: datetime,
        created_at: datetime,
    ) -> CommandRecord:
        from tennis_engine.common.ids import stable_id

        command_id = stable_id("ingestion-command", idempotency_key)
        with self.engine.begin() as opened:
            opened.execute(
                text(
                    "INSERT INTO tennis.ingestion_command "
                    "(command_id,idempotency_key,source_id,logical_resource_id,"
                    "observation_window,created_at) "
                    "VALUES (:command_id,:idempotency_key,:source_id,:resource_id,"
                    ":window,:created) "
                    "ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                {
                    "command_id": command_id,
                    "idempotency_key": idempotency_key,
                    "source_id": source_id,
                    "resource_id": logical_resource_id,
                    "window": observation_window,
                    "created": created_at,
                },
            )
            row = opened.execute(
                text(
                    "SELECT command_id,idempotency_key,source_id,logical_resource_id,"
                    "observation_window,created_at FROM tennis.ingestion_command "
                    "WHERE idempotency_key=:key"
                ),
                {"key": idempotency_key},
            ).one()
        data = row._mapping
        if (
            data["source_id"] != source_id
            or data["logical_resource_id"] != logical_resource_id
            or data["observation_window"] != observation_window
        ):
            raise ValueError("Idempotency key was already used for a different command")
        return CommandRecord(
            command_id=data["command_id"],
            idempotency_key=data["idempotency_key"],
            source_id=data["source_id"],
            logical_resource_id=data["logical_resource_id"],
            observation_window=data["observation_window"],
            created_at=data["created_at"],
        )

    def stage_content(self, content: ContentRecord) -> ContentRecord:
        with self.engine.begin() as opened:
            inserted = opened.execute(
                text(
                    "INSERT INTO tennis.raw_content "
                    "(content_id,source_id,content_sha256,object_key,content_type,size_bytes,"
                    "archive_state,created_at,updated_at) VALUES "
                    "(:id,:source,:sha,:key,:content_type,:size,:state,:created,:created) "
                    "ON CONFLICT (source_id,content_sha256) DO NOTHING RETURNING content_id"
                ),
                {
                    "id": content.content_id,
                    "source": content.source_id,
                    "sha": content.body_sha256,
                    "key": content.object_key,
                    "content_type": content.content_type,
                    "size": content.size_bytes,
                    "state": content.state.value,
                    "created": content.created_at,
                },
            ).scalar_one_or_none()
            if inserted is not None:
                opened.execute(
                    text(
                        "INSERT INTO tennis.raw_archive_transition "
                        "(content_id,previous_state,archive_state,reason,recorded_at) "
                        "VALUES (:id,NULL,:state,'archive staged',:at)"
                    ),
                    {
                        "id": content.content_id,
                        "state": content.state.value,
                        "at": content.created_at,
                    },
                )
            row = opened.execute(
                text(
                    "SELECT * FROM tennis.raw_content "
                    "WHERE source_id=:source AND content_sha256=:sha"
                ),
                {"source": content.source_id, "sha": content.body_sha256},
            ).one()
        existing = _content(row)
        if (
            existing.object_key != content.object_key
            or existing.size_bytes != content.size_bytes
            or existing.content_type != content.content_type
        ):
            raise ValueError("Stored content identity has incompatible metadata")
        return existing

    def set_content_state(
        self, content_id: UUID, state: ArchiveState, *, reason: str, recorded_at: datetime
    ) -> ContentRecord:
        with self.engine.begin() as opened:
            row = opened.execute(
                text("SELECT * FROM tennis.raw_content WHERE content_id=:id FOR UPDATE"),
                {"id": content_id},
            ).one()
            existing = _content(row)
            if existing.state == state:
                return existing
            allowed = {
                ArchiveState.PENDING: {ArchiveState.ARCHIVED, ArchiveState.FAILED},
                ArchiveState.ARCHIVED: {ArchiveState.FAILED},
                ArchiveState.FAILED: {ArchiveState.ARCHIVED},
            }
            if state not in allowed[existing.state]:
                raise ValueError(f"Invalid archive transition {existing.state} -> {state}")
            opened.execute(
                text(
                    "UPDATE tennis.raw_content SET archive_state=:state,updated_at=:at "
                    "WHERE content_id=:id"
                ),
                {"state": state.value, "at": recorded_at, "id": content_id},
            )
            opened.execute(
                text(
                    "INSERT INTO tennis.raw_archive_transition "
                    "(content_id,previous_state,archive_state,reason,recorded_at) "
                    "VALUES (:id,:previous,:state,:reason,:at)"
                ),
                {
                    "id": content_id,
                    "previous": existing.state.value,
                    "state": state.value,
                    "reason": reason,
                    "at": recorded_at,
                },
            )
        return existing.model_copy(update={"state": state})

    def content_by_id(self, content_id: UUID) -> ContentRecord:
        with self.engine.connect() as opened:
            row = opened.execute(
                text("SELECT * FROM tennis.raw_content WHERE content_id=:id"), {"id": content_id}
            ).one()
        return _content(row)

    def contents(self) -> Sequence[ContentRecord]:
        with self.engine.connect() as opened:
            rows = opened.execute(
                text("SELECT * FROM tennis.raw_content ORDER BY created_at")
            ).all()
        return tuple(_content(row) for row in rows)

    def record_fetch(self, result: FetchResult) -> FetchResult:
        with self.engine.begin() as opened:
            opened.execute(
                text(
                    "INSERT INTO tennis.fetch_attempt "
                    "(attempt_id,command_id,source_id,logical_resource_id,request_identity,"
                    "requested_at,completed_at,origin,disposition,attempt_number,status_code,"
                    "content_type,raw_content_id,etag,last_modified,retry_after_seconds,"
                    "cache_control,provider_request_id,parser_candidate,error_code,"
                    "policy_version,policy_revision) VALUES "
                    "(:attempt,:command,:source,:resource,:request,:requested,:completed,:origin,"
                    ":disposition,:number,:status,:content_type,:content,:etag,:last_modified,"
                    ":retry_after,:cache_control,:provider_request_id,:parser,:error,"
                    ":policy_version,:policy_revision) "
                    "ON CONFLICT (attempt_id) DO NOTHING"
                ),
                {
                    "attempt": result.attempt_id,
                    "command": result.command_id,
                    "source": result.source_id,
                    "resource": result.logical_resource_id,
                    "request": result.request_identity,
                    "requested": result.requested_at,
                    "completed": result.completed_at,
                    "origin": result.origin.value,
                    "disposition": result.disposition.value,
                    "number": result.attempt_number,
                    "status": result.status_code,
                    "content_type": result.content_type,
                    "content": result.raw_content_id,
                    "etag": result.etag,
                    "last_modified": result.last_modified,
                    "retry_after": result.retry_after_seconds,
                    "cache_control": result.cache_control,
                    "provider_request_id": result.provider_request_id,
                    "parser": result.parser_candidate,
                    "error": result.error_code.value if result.error_code else None,
                    "policy_version": result.policy_version,
                    "policy_revision": result.policy_revision,
                },
            )
        return result

    def add_observation(self, observation: ObservationRecord) -> ObservationRecord:
        with self.engine.begin() as opened:
            opened.execute(
                text(
                    "INSERT INTO tennis.source_observation "
                    "(observation_id,attempt_id,command_id,source_id,logical_resource_id,"
                    "raw_content_id,observed_at,effective_at) VALUES "
                    "(:observation,:attempt,:command,:source,:resource,:content,"
                    ":observed,:effective) "
                    "ON CONFLICT (observation_id) DO NOTHING"
                ),
                {
                    "observation": observation.observation_id,
                    "attempt": observation.attempt_id,
                    "command": observation.command_id,
                    "source": observation.source_id,
                    "resource": observation.logical_resource_id,
                    "content": observation.raw_content_id,
                    "observed": observation.observed_at,
                    "effective": observation.effective_at,
                },
            )
        return observation

    def observation(self, observation_id: UUID) -> ReplaySelection:
        with self.engine.connect() as opened:
            row = opened.execute(
                text(
                    "SELECT o.*,c.content_sha256,c.object_key,c.content_type,c.size_bytes,"
                    "c.archive_state,c.created_at AS content_created_at "
                    "FROM tennis.source_observation o JOIN tennis.raw_content c "
                    "ON c.content_id=o.raw_content_id WHERE o.observation_id=:id"
                ),
                {"id": observation_id},
            ).one()
        return self._selection(row)

    def select_replay(self, request: ReplayRequest) -> Sequence[ReplaySelection]:
        clauses = ["1=1"]
        params: dict[str, Any] = {}
        if request.source_id is not None:
            clauses.append("o.source_id=:source")
            params["source"] = request.source_id
        if request.logical_resource_id is not None:
            clauses.append("o.logical_resource_id=:resource")
            params["resource"] = request.logical_resource_id
        if request.observed_from is not None:
            clauses.append("o.observed_at>=:observed_from")
            params["observed_from"] = request.observed_from
        if request.observed_to is not None:
            clauses.append("o.observed_at<:observed_to")
            params["observed_to"] = request.observed_to
        query = (
            "SELECT o.*,c.content_sha256,c.object_key,c.content_type,c.size_bytes,"
            "c.archive_state,c.created_at AS content_created_at "
            "FROM tennis.source_observation o JOIN tennis.raw_content c "
            "ON c.content_id=o.raw_content_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY o.observed_at,o.observation_id"
        )
        with self.engine.connect() as opened:
            rows = opened.execute(text(query), params).all()
        return tuple(self._selection(row) for row in rows)

    @staticmethod
    def _selection(row: Any) -> ReplaySelection:
        data = row._mapping
        return ReplaySelection(
            observation=ObservationRecord(
                observation_id=data["observation_id"],
                attempt_id=data["attempt_id"],
                command_id=data["command_id"],
                source_id=data["source_id"],
                logical_resource_id=data["logical_resource_id"],
                raw_content_id=data["raw_content_id"],
                observed_at=data["observed_at"],
                effective_at=data["effective_at"],
            ),
            content=ContentRecord(
                content_id=data["raw_content_id"],
                source_id=data["source_id"],
                body_sha256=data["content_sha256"],
                object_key=data["object_key"],
                content_type=data["content_type"],
                size_bytes=data["size_bytes"],
                state=data["archive_state"],
                created_at=data["content_created_at"],
            ),
        )

    def record_parse(self, result: ParseResult) -> ParseResult:
        with self.engine.begin() as opened:
            self._insert_parse(opened, result)
        return result

    def write_derived(self, record: DerivedRecord) -> tuple[DerivedRecord, bool]:
        with self.engine.begin() as opened:
            return self._insert_derived(opened, record)

    def commit_parse(self, result: ParseResult, records: Sequence[DerivedRecord]) -> ParseResult:
        if tuple(record.record_id for record in records) != result.derived_record_ids:
            raise ValueError("Parse result and derived records differ")
        with self.engine.begin() as opened:
            for record in records:
                stored, _ = self._insert_derived(opened, record)
                if stored.record_id != record.record_id:
                    raise ValueError("Derived identity resolved to an unexpected record")
            self._insert_parse(opened, result)
        return result

    @staticmethod
    def _insert_parse(opened: Connection, result: ParseResult) -> None:
        opened.execute(
            text(
                "INSERT INTO tennis.parsing_attempt "
                "(parse_attempt_id,observation_id,parser_version,status,derived_record_ids,"
                "errors,recorded_at) VALUES "
                "(:attempt,:observation,:parser,:status,CAST(:derived AS jsonb),"
                "CAST(:errors AS jsonb),now()) ON CONFLICT (parse_attempt_id) DO NOTHING"
            ),
            {
                "attempt": result.parse_attempt_id,
                "observation": result.observation_id,
                "parser": result.parser_version,
                "status": result.status.value,
                "derived": _json([str(item) for item in result.derived_record_ids]),
                "errors": _json(list(result.errors)),
            },
        )

    @staticmethod
    def _insert_derived(opened: Connection, record: DerivedRecord) -> tuple[DerivedRecord, bool]:
        inserted = opened.execute(
            text(
                "INSERT INTO tennis.derived_record "
                "(record_id,source_id,record_type,natural_key,parser_version,payload,"
                "payload_sha256,raw_content_id,observation_id,parse_attempt_id,created_at) "
                "VALUES (:id,:source,:type,:key,:parser,CAST(:payload AS jsonb),:sha,:content,"
                ":observation,:parse_attempt,:created) ON CONFLICT "
                "(source_id,record_type,natural_key,parser_version,payload_sha256) "
                "DO NOTHING RETURNING record_id"
            ),
            {
                "id": record.record_id,
                "source": record.source_id,
                "type": record.record_type,
                "key": record.natural_key,
                "parser": record.parser_version,
                "payload": _json(record.payload),
                "sha": record.payload_sha256,
                "content": record.raw_content_id,
                "observation": record.observation_id,
                "parse_attempt": record.parse_attempt_id,
                "created": record.created_at,
            },
        ).scalar_one_or_none()
        if inserted is not None:
            return record, True
        row = opened.execute(
            text(
                "SELECT * FROM tennis.derived_record WHERE source_id=:source "
                "AND record_type=:type AND natural_key=:key AND parser_version=:parser "
                "AND payload_sha256=:sha"
            ),
            {
                "source": record.source_id,
                "type": record.record_type,
                "key": record.natural_key,
                "parser": record.parser_version,
                "sha": record.payload_sha256,
            },
        ).one()
        data = row._mapping
        payload = cast(dict[str, Any], data["payload"])
        return (
            DerivedRecord(
                record_id=data["record_id"],
                source_id=data["source_id"],
                record_type=data["record_type"],
                natural_key=data["natural_key"],
                parser_version=data["parser_version"],
                payload=payload,
                payload_sha256=data["payload_sha256"],
                raw_content_id=data["raw_content_id"],
                observation_id=data["observation_id"],
                parse_attempt_id=data["parse_attempt_id"],
                created_at=data["created_at"],
            ),
            False,
        )

    def record_dead_letter(self, revision: DeadLetterRevision) -> None:
        with self.engine.begin() as opened:
            opened.execute(
                text(
                    "INSERT INTO tennis.ingestion_dead_letter_revision "
                    "(dead_letter_id,source_id,raw_content_id,parser_version,errors,retry_history,"
                    "resolution_status,replay_job_id,recorded_at) VALUES "
                    "(:id,:source,:content,:parser,CAST(:errors AS jsonb),CAST(:history AS jsonb),"
                    ":status,:replay,:recorded)"
                ),
                {
                    "id": revision.dead_letter_id,
                    "source": revision.source_id,
                    "content": revision.raw_content_id,
                    "parser": revision.parser_version,
                    "errors": _json(list(revision.errors)),
                    "history": _json(list(revision.retry_history)),
                    "status": revision.status.value,
                    "replay": revision.replay_job_id,
                    "recorded": revision.recorded_at,
                },
            )

    def open_dead_letters(self, raw_content_id: UUID) -> Sequence[DeadLetterRevision]:
        with self.engine.connect() as opened:
            rows = opened.execute(
                text(
                    "SELECT * FROM (SELECT DISTINCT ON (dead_letter_id) * "
                    "FROM tennis.ingestion_dead_letter_revision WHERE raw_content_id=:content "
                    "ORDER BY dead_letter_id,revision DESC) latest "
                    "WHERE resolution_status='OPEN' ORDER BY recorded_at"
                ),
                {"content": raw_content_id},
            ).all()
        return tuple(
            DeadLetterRevision(
                dead_letter_id=row._mapping["dead_letter_id"],
                source_id=row._mapping["source_id"],
                raw_content_id=row._mapping["raw_content_id"],
                parser_version=row._mapping["parser_version"],
                errors=tuple(row._mapping["errors"]),
                retry_history=tuple(row._mapping["retry_history"]),
                status=DeadLetterStatus(row._mapping["resolution_status"]),
                replay_job_id=row._mapping["replay_job_id"],
                recorded_at=row._mapping["recorded_at"],
            )
            for row in rows
        )

    def record_replay_started(
        self, replay_job_id: UUID, request: ReplayRequest, started_at: datetime
    ) -> None:
        with self.engine.begin() as opened:
            opened.execute(
                text(
                    "INSERT INTO tennis.replay_job_revision "
                    "(replay_job_id,request,status,started_at,recorded_at) VALUES "
                    "(:id,CAST(:request AS jsonb),'RUNNING',:started,:started)"
                ),
                {
                    "id": replay_job_id,
                    "request": request.model_dump_json(),
                    "started": started_at,
                },
            )

    def record_replay(self, result: ReplayResult) -> None:
        with self.engine.begin() as opened:
            opened.execute(
                text(
                    "INSERT INTO tennis.replay_job_revision "
                    "(replay_job_id,request,status,started_at,completed_at,selected_observations,"
                    "accepted,rejected,derived_records,recorded_at) VALUES "
                    "(:id,CAST(:request AS jsonb),'COMPLETED',:started,:completed,:selected,"
                    ":accepted,:rejected,:derived,:completed)"
                ),
                {
                    "id": result.replay_job_id,
                    "request": result.request.model_dump_json(),
                    "started": result.started_at,
                    "completed": result.completed_at,
                    "selected": result.selected_observations,
                    "accepted": result.accepted,
                    "rejected": result.rejected,
                    "derived": result.derived_records,
                },
            )

    def record_orphan(self, orphan: OrphanObject) -> None:
        with self.engine.begin() as opened:
            opened.execute(
                text(
                    "INSERT INTO tennis.orphan_object(object_key,reason,recorded_at) "
                    "VALUES (:key,:reason,:at) ON CONFLICT (object_key) DO NOTHING"
                ),
                {"key": orphan.object_key, "reason": orphan.reason, "at": orphan.recorded_at},
            )

    def tracked_object_keys(self) -> set[str]:
        with self.engine.connect() as opened:
            keys = opened.execute(text("SELECT object_key FROM tennis.raw_content")).scalars()
            return set(keys)

    def record_runtime_event(self, event: SourceRuntimeEvent) -> None:
        with self.engine.begin() as opened:
            opened.execute(
                text(
                    "INSERT INTO tennis.source_runtime_event "
                    "(event_id,source_id,event_type,until,details,recorded_at) "
                    "VALUES (:id,:source,:type,:until,CAST(:details AS jsonb),:at) "
                    "ON CONFLICT (event_id) DO NOTHING"
                ),
                {
                    "id": event.event_id,
                    "source": event.source_id,
                    "type": event.event_type,
                    "until": event.until,
                    "details": _json(event.details),
                    "at": event.recorded_at,
                },
            )

    def latest_runtime_event(self, source_id: str) -> SourceRuntimeEvent | None:
        with self.engine.connect() as opened:
            row = opened.execute(
                text(
                    "SELECT * FROM tennis.source_runtime_event WHERE source_id=:source "
                    "ORDER BY recorded_at DESC,event_id DESC LIMIT 1"
                ),
                {"source": source_id},
            ).one_or_none()
        if row is None:
            return None
        data = row._mapping
        return SourceRuntimeEvent(
            event_id=data["event_id"],
            source_id=data["source_id"],
            event_type=data["event_type"],
            until=data["until"],
            details=cast(dict[str, Any], data["details"]),
            recorded_at=data["recorded_at"],
        )
