"""Archive-before-parse ingestion, idempotent derived writes, replay, and recovery."""

import gzip
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from tennis_engine.common.clock import Clock
from tennis_engine.common.ids import new_id, stable_id
from tennis_engine.infrastructure.object_store import ImmutableObjectStore

from .contracts import (
    ArchiveState,
    ContentRecord,
    DeadLetterStatus,
    FetchCapture,
    FetchDisposition,
    FetchErrorCode,
    FetchResult,
    ObservationRecord,
    ParseResult,
    ParseStatus,
    ReplayRequest,
    ReplayResult,
    ReplaySelection,
)
from .parser import ParserRegistry, SourceParser
from .store import DeadLetterRevision, DerivedRecord, IngestionStore, OrphanObject


class ArchiveError(RuntimeError):
    pass


class ParseRejected(ValueError):
    def __init__(self, result: ParseResult):
        self.result = result
        super().__init__("; ".join(result.errors))


def canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def raw_object_key(source_id: str, sha256: str, content_type: str | None) -> str:
    suffix = "json" if content_type and "json" in content_type.casefold() else "bin"
    return f"raw/{source_id}/{sha256[:2]}/{sha256}.{suffix}.gz"


def _compressed(content: bytes) -> bytes:
    return gzip.compress(content, compresslevel=6, mtime=0)


class IngestionService:
    def __init__(
        self,
        repository: IngestionStore,
        objects: ImmutableObjectStore,
        clock: Clock,
    ) -> None:
        self.repository = repository
        self.objects = objects
        self.clock = clock

    def archive_attempts(
        self,
        *,
        idempotency_key: str,
        observation_window: datetime,
        captures: tuple[FetchCapture, ...],
        parser_candidate: str,
        policy_version: str,
        policy_revision: int,
        effective_at: datetime | None = None,
    ) -> tuple[FetchResult, ...]:
        if not captures:
            raise ValueError("An ingestion command requires at least one fetch attempt")
        identities = {(item.source_id, item.logical_resource_id) for item in captures}
        attempts = [item.attempt_number for item in captures]
        if len(identities) != 1 or len(set(attempts)) != len(attempts):
            raise ValueError(
                "Fetch attempts must describe one resource with unique attempt numbers"
            )
        return tuple(
            self.archive(
                idempotency_key=idempotency_key,
                observation_window=observation_window,
                capture=capture,
                parser_candidate=parser_candidate,
                policy_version=policy_version,
                policy_revision=policy_revision,
                effective_at=effective_at,
            )
            for capture in sorted(captures, key=lambda item: item.attempt_number)
        )

    def archive(
        self,
        *,
        idempotency_key: str,
        observation_window: datetime,
        capture: FetchCapture,
        parser_candidate: str,
        policy_version: str,
        policy_revision: int,
        effective_at: datetime | None = None,
    ) -> FetchResult:
        command = self.repository.start_command(
            idempotency_key=idempotency_key,
            source_id=capture.source_id,
            logical_resource_id=capture.logical_resource_id,
            observation_window=observation_window,
            created_at=self.clock.now(),
        )
        attempt_id = stable_id("fetch-attempt", f"{command.command_id}:{capture.attempt_number}")
        content: ContentRecord | None = None
        object_key: str | None = None
        digest: str | None = None
        disposition = capture.disposition
        error_code = capture.error_code
        if capture.body is not None:
            digest = hashlib.sha256(capture.body).hexdigest()
            object_key = raw_object_key(capture.source_id, digest, capture.content_type)
            proposed = ContentRecord(
                content_id=stable_id("raw-content", f"{capture.source_id}:{digest}"),
                source_id=capture.source_id,
                body_sha256=digest,
                object_key=object_key,
                content_type=capture.content_type or "application/octet-stream",
                size_bytes=len(capture.body),
                state=ArchiveState.PENDING,
                created_at=self.clock.now(),
            )
            content = self.repository.stage_content(proposed)
            if content.state != ArchiveState.ARCHIVED:
                try:
                    self.objects.put(content.object_key, _compressed(capture.body))
                    self._verify(content, expected=capture.body)
                    content = self.repository.set_content_state(
                        content.content_id,
                        ArchiveState.ARCHIVED,
                        reason="bytes stored and hash verified",
                        recorded_at=self.clock.now(),
                    )
                except Exception as error:
                    content = self.repository.set_content_state(
                        content.content_id,
                        ArchiveState.FAILED,
                        reason=f"{type(error).__name__}: archive verification failed",
                        recorded_at=self.clock.now(),
                    )
                    disposition = FetchDisposition.STORAGE_FAILURE
                    error_code = FetchErrorCode.STORAGE_ERROR
                    self._dead_letter(
                        source_id=capture.source_id,
                        raw_content_id=content.content_id,
                        parser_version=parser_candidate,
                        errors=(f"{type(error).__name__}: archive verification failed",),
                    )
        observation_id: UUID | None = None
        if (
            capture.refreshes_observation
            and content is not None
            and content.state == ArchiveState.ARCHIVED
        ):
            observation_id = stable_id("observation", str(attempt_id))
        result = FetchResult(
            attempt_id=attempt_id,
            command_id=command.command_id,
            source_id=capture.source_id,
            logical_resource_id=capture.logical_resource_id,
            request_identity=capture.request_identity,
            requested_at=capture.requested_at,
            completed_at=capture.completed_at,
            origin=capture.origin,
            disposition=disposition,
            attempt_number=capture.attempt_number,
            status_code=capture.status_code,
            content_type=capture.content_type,
            body_sha256=digest,
            raw_content_id=content.content_id if content is not None else None,
            object_key=object_key,
            observation_id=observation_id,
            etag=capture.etag,
            last_modified=capture.last_modified,
            cache_control=capture.cache_control,
            provider_request_id=capture.provider_request_id,
            retry_after_seconds=capture.retry_after_seconds,
            parser_candidate=parser_candidate,
            error_code=error_code,
            policy_version=policy_version,
            policy_revision=policy_revision,
        )
        self.repository.record_fetch(result)
        if observation_id is not None and content is not None:
            self.repository.add_observation(
                ObservationRecord(
                    observation_id=observation_id,
                    attempt_id=attempt_id,
                    command_id=command.command_id,
                    source_id=capture.source_id,
                    logical_resource_id=capture.logical_resource_id,
                    raw_content_id=content.content_id,
                    observed_at=capture.completed_at,
                    effective_at=effective_at,
                )
            )
        if disposition == FetchDisposition.STORAGE_FAILURE:
            raise ArchiveError("Raw bytes were not durably archived; checkpoint must not advance")
        return result

    def _verify(self, content: ContentRecord, *, expected: bytes | None = None) -> bytes:
        archived = self.objects.get(content.object_key)
        raw = gzip.decompress(archived)
        if hashlib.sha256(raw).hexdigest() != content.body_sha256:
            raise ArchiveError("Archived body hash does not match staged metadata")
        if expected is not None and raw != expected:
            raise ArchiveError("Archived bytes differ from fetched bytes")
        return raw

    def parse_observation(
        self,
        selection: ReplaySelection,
        parser: SourceParser,
        *,
        replay_job_id: UUID | None = None,
        dry_run: bool = False,
    ) -> ParseResult:
        parse_attempt_id = new_id()
        errors: tuple[str, ...] = ()
        derived_ids: list[UUID] = []
        derived_records: list[DerivedRecord] = []
        try:
            if selection.content.state != ArchiveState.ARCHIVED:
                raise ArchiveError("Raw content is not in ARCHIVED state")
            raw = self._verify(selection.content)
            parsed = parser.parse(raw)
            if not parsed:
                raise ValueError("Parser produced an unexpected empty result")
            for item in parsed:
                encoded = canonical_json(item.payload)
                payload_sha256 = hashlib.sha256(encoded).hexdigest()
                record_id = stable_id(
                    "derived-record",
                    ":".join(
                        (
                            selection.observation.source_id,
                            item.record_type,
                            item.natural_key,
                            parser.version,
                            payload_sha256,
                        )
                    ),
                )
                derived_records.append(
                    DerivedRecord(
                        record_id=record_id,
                        source_id=selection.observation.source_id,
                        record_type=item.record_type,
                        natural_key=item.natural_key,
                        parser_version=parser.version,
                        payload=item.payload,
                        payload_sha256=payload_sha256,
                        raw_content_id=selection.content.content_id,
                        observation_id=selection.observation.observation_id,
                        parse_attempt_id=parse_attempt_id,
                        created_at=self.clock.now(),
                    )
                )
                derived_ids.append(record_id)
            status = ParseStatus.DRY_RUN if dry_run else ParseStatus.ACCEPTED
        except Exception as error:
            status = ParseStatus.REJECTED
            errors = (f"{type(error).__name__}: {error}",)
            derived_ids.clear()
            derived_records.clear()
            if not dry_run:
                self._dead_letter(
                    source_id=selection.observation.source_id,
                    raw_content_id=selection.content.content_id,
                    parser_version=parser.version,
                    errors=errors,
                    replay_job_id=replay_job_id,
                )
        result = ParseResult(
            parse_attempt_id=parse_attempt_id,
            observation_id=selection.observation.observation_id,
            parser_version=parser.version,
            status=status,
            derived_record_ids=tuple(derived_ids),
            errors=errors,
        )
        if status == ParseStatus.ACCEPTED:
            self.repository.commit_parse(result, derived_records)
        else:
            self.repository.record_parse(result)
        if status == ParseStatus.REJECTED:
            raise ParseRejected(result)
        if replay_job_id is not None and not dry_run:
            for open_item in self.repository.open_dead_letters(selection.content.content_id):
                self.repository.record_dead_letter(
                    DeadLetterRevision(
                        dead_letter_id=open_item.dead_letter_id,
                        source_id=open_item.source_id,
                        raw_content_id=open_item.raw_content_id,
                        parser_version=parser.version,
                        errors=(),
                        retry_history=open_item.retry_history
                        + (f"replay:{replay_job_id}:{parser.version}",),
                        status=DeadLetterStatus.RESOLVED,
                        replay_job_id=replay_job_id,
                        recorded_at=self.clock.now(),
                    )
                )
        return result

    def replay(self, request: ReplayRequest, registry: ParserRegistry) -> ReplayResult:
        parser = registry.get(request.parser_version)
        job_id = new_id()
        started_at = self.clock.now()
        self.repository.record_replay_started(job_id, request, started_at)
        selections = self.repository.select_replay(request)
        accepted = rejected = derived = 0
        for selection in selections:
            try:
                result = self.parse_observation(
                    selection, parser, replay_job_id=job_id, dry_run=request.dry_run
                )
                accepted += 1
                derived += len(result.derived_record_ids)
            except ParseRejected:
                rejected += 1
        replay = ReplayResult(
            replay_job_id=job_id,
            request=request,
            started_at=started_at,
            completed_at=self.clock.now(),
            selected_observations=len(selections),
            accepted=accepted,
            rejected=rejected,
            derived_records=derived,
        )
        self.repository.record_replay(replay)
        return replay

    def reconcile(self) -> dict[str, int]:
        recovered = failed = orphans = 0
        for content in self.repository.contents():
            try:
                if not self.objects.exists(content.object_key):
                    raise FileNotFoundError(content.object_key)
                self._verify(content)
                if content.state != ArchiveState.ARCHIVED:
                    self.repository.set_content_state(
                        content.content_id,
                        ArchiveState.ARCHIVED,
                        reason="reconciliation verified staged bytes",
                        recorded_at=self.clock.now(),
                    )
                    recovered += 1
            except Exception as error:
                if content.state != ArchiveState.FAILED:
                    self.repository.set_content_state(
                        content.content_id,
                        ArchiveState.FAILED,
                        reason=f"{type(error).__name__}: reconciliation failed",
                        recorded_at=self.clock.now(),
                    )
                self._dead_letter(
                    source_id=content.source_id,
                    raw_content_id=content.content_id,
                    parser_version=None,
                    errors=(f"{type(error).__name__}: reconciliation failed",),
                )
                failed += 1
        tracked = self.repository.tracked_object_keys()
        for key in self.objects.list_keys("raw/"):
            if key not in tracked:
                self.repository.record_orphan(
                    OrphanObject(key, "object has no raw_content row", self.clock.now())
                )
                orphans += 1
        return {"recovered": recovered, "failed": failed, "orphans": orphans}

    def _dead_letter(
        self,
        *,
        source_id: str,
        raw_content_id: UUID | None,
        parser_version: str | None,
        errors: tuple[str, ...],
        replay_job_id: UUID | None = None,
    ) -> None:
        identity = f"{source_id}:{raw_content_id}:{parser_version}"
        self.repository.record_dead_letter(
            DeadLetterRevision(
                dead_letter_id=stable_id("dead-letter", identity),
                source_id=source_id,
                raw_content_id=raw_content_id,
                parser_version=parser_version,
                errors=errors,
                retry_history=(),
                status=DeadLetterStatus.OPEN,
                replay_job_id=replay_job_id,
                recorded_at=self.clock.now(),
            )
        )


def safe_filename(value: str) -> str:
    """Small helper used by fixture tools; never use a resource ID as a raw path."""
    name = PurePosixPath(value).name
    if name != value or name in {"", ".", ".."}:
        raise ValueError("Expected a single safe path component")
    return name


ParserFactory = Callable[[], SourceParser]
