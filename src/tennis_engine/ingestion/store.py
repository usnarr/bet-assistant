"""Persistence boundary plus a deterministic in-memory implementation for SYS-03."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from tennis_engine.common.ids import stable_id

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
    in_half_open_interval,
)


@dataclass(frozen=True)
class CommandRecord:
    command_id: UUID
    idempotency_key: str
    source_id: str
    logical_resource_id: str
    observation_window: datetime
    created_at: datetime


@dataclass(frozen=True)
class DerivedRecord:
    record_id: UUID
    source_id: str
    record_type: str
    natural_key: str
    parser_version: str
    payload: dict[str, Any]
    payload_sha256: str
    raw_content_id: UUID
    observation_id: UUID
    parse_attempt_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class DeadLetterRevision:
    dead_letter_id: UUID
    source_id: str
    raw_content_id: UUID | None
    parser_version: str | None
    errors: tuple[str, ...]
    retry_history: tuple[str, ...]
    status: DeadLetterStatus
    replay_job_id: UUID | None
    recorded_at: datetime


@dataclass(frozen=True)
class ArchiveTransition:
    content_id: UUID
    previous_state: ArchiveState | None
    state: ArchiveState
    reason: str
    recorded_at: datetime


@dataclass(frozen=True)
class OrphanObject:
    object_key: str
    reason: str
    recorded_at: datetime


@dataclass(frozen=True)
class SourceRuntimeEvent:
    event_id: UUID
    source_id: str
    event_type: str
    until: datetime | None
    details: dict[str, Any]
    recorded_at: datetime


class IngestionStore(Protocol):
    def start_command(
        self,
        *,
        idempotency_key: str,
        source_id: str,
        logical_resource_id: str,
        observation_window: datetime,
        created_at: datetime,
    ) -> CommandRecord: ...

    def stage_content(self, content: ContentRecord) -> ContentRecord: ...
    def set_content_state(
        self, content_id: UUID, state: ArchiveState, *, reason: str, recorded_at: datetime
    ) -> ContentRecord: ...
    def content_by_id(self, content_id: UUID) -> ContentRecord: ...
    def contents(self) -> Sequence[ContentRecord]: ...
    def record_fetch(self, result: FetchResult) -> FetchResult: ...
    def add_observation(self, observation: ObservationRecord) -> ObservationRecord: ...
    def observation(self, observation_id: UUID) -> ReplaySelection: ...
    def select_replay(self, request: ReplayRequest) -> Sequence[ReplaySelection]: ...
    def record_parse(self, result: ParseResult) -> ParseResult: ...
    def write_derived(self, record: DerivedRecord) -> tuple[DerivedRecord, bool]: ...
    def commit_parse(
        self, result: ParseResult, records: Sequence[DerivedRecord]
    ) -> ParseResult: ...
    def record_dead_letter(self, revision: DeadLetterRevision) -> None: ...
    def open_dead_letters(self, raw_content_id: UUID) -> Sequence[DeadLetterRevision]: ...
    def record_replay_started(
        self, replay_job_id: UUID, request: ReplayRequest, started_at: datetime
    ) -> None: ...
    def record_replay(self, result: ReplayResult) -> None: ...
    def record_orphan(self, orphan: OrphanObject) -> None: ...
    def tracked_object_keys(self) -> set[str]: ...
    def record_runtime_event(self, event: SourceRuntimeEvent) -> None: ...
    def latest_runtime_event(self, source_id: str) -> SourceRuntimeEvent | None: ...


class MemoryIngestionStore:
    """Faithful idempotency model used by unit tests and local fixture demonstrations."""

    def __init__(self) -> None:
        self.commands_by_key: dict[str, CommandRecord] = {}
        self.content_by_identity: dict[tuple[str, str], ContentRecord] = {}
        self.content_records: dict[UUID, ContentRecord] = {}
        self.archive_transitions: list[ArchiveTransition] = []
        self.fetches: dict[UUID, FetchResult] = {}
        self.observations: dict[UUID, ObservationRecord] = {}
        self.parse_results: dict[UUID, ParseResult] = {}
        self.derived_records: dict[UUID, DerivedRecord] = {}
        self.derived_by_identity: dict[tuple[str, str, str, str, str], UUID] = {}
        self.dead_letters: list[DeadLetterRevision] = []
        self.replay_jobs: dict[UUID, ReplayResult] = {}
        self.replay_starts: dict[UUID, tuple[ReplayRequest, datetime]] = {}
        self.orphans: dict[str, OrphanObject] = {}
        self.runtime_events: list[SourceRuntimeEvent] = []

    def start_command(
        self,
        *,
        idempotency_key: str,
        source_id: str,
        logical_resource_id: str,
        observation_window: datetime,
        created_at: datetime,
    ) -> CommandRecord:
        command_id = stable_id("ingestion-command", idempotency_key)
        proposed = CommandRecord(
            command_id,
            idempotency_key,
            source_id,
            logical_resource_id,
            observation_window,
            created_at,
        )
        existing = self.commands_by_key.get(idempotency_key)
        if existing is not None:
            comparable = replace(existing, created_at=created_at)
            if comparable != proposed:
                raise ValueError("Idempotency key was already used for a different command")
            return existing
        self.commands_by_key[idempotency_key] = proposed
        return proposed

    def stage_content(self, content: ContentRecord) -> ContentRecord:
        identity = (content.source_id, content.body_sha256)
        existing = self.content_by_identity.get(identity)
        if existing is not None:
            return existing
        if content.content_id in self.content_records:
            raise ValueError("Content ID collision")
        self.content_by_identity[identity] = content
        self.content_records[content.content_id] = content
        self.archive_transitions.append(
            ArchiveTransition(
                content.content_id, None, content.state, "archive staged", content.created_at
            )
        )
        return content

    def set_content_state(
        self, content_id: UUID, state: ArchiveState, *, reason: str, recorded_at: datetime
    ) -> ContentRecord:
        existing = self.content_by_id(content_id)
        if existing.state == state:
            return existing
        allowed = {
            ArchiveState.PENDING: {ArchiveState.ARCHIVED, ArchiveState.FAILED},
            ArchiveState.ARCHIVED: {ArchiveState.FAILED},
            ArchiveState.FAILED: {ArchiveState.ARCHIVED},
        }
        if state not in allowed[existing.state]:
            raise ValueError(f"Invalid archive transition {existing.state} -> {state}")
        changed = existing.model_copy(update={"state": state})
        self.content_records[content_id] = changed
        self.content_by_identity[(changed.source_id, changed.body_sha256)] = changed
        self.archive_transitions.append(
            ArchiveTransition(content_id, existing.state, state, reason, recorded_at)
        )
        return changed

    def content_by_id(self, content_id: UUID) -> ContentRecord:
        try:
            return self.content_records[content_id]
        except KeyError as error:
            raise KeyError(f"Unknown content {content_id}") from error

    def contents(self) -> Sequence[ContentRecord]:
        return tuple(self.content_records.values())

    def record_fetch(self, result: FetchResult) -> FetchResult:
        existing = self.fetches.get(result.attempt_id)
        if existing is not None and existing != result:
            raise ValueError("Attempt ID collision")
        self.fetches[result.attempt_id] = result
        return result

    def add_observation(self, observation: ObservationRecord) -> ObservationRecord:
        existing = self.observations.get(observation.observation_id)
        if existing is not None and existing != observation:
            raise ValueError("Observation ID collision")
        self.observations[observation.observation_id] = observation
        return observation

    def observation(self, observation_id: UUID) -> ReplaySelection:
        observation = self.observations[observation_id]
        return ReplaySelection(
            observation=observation,
            content=self.content_by_id(observation.raw_content_id),
        )

    def select_replay(self, request: ReplayRequest) -> Sequence[ReplaySelection]:
        selected = []
        for observation in self.observations.values():
            if request.source_id is not None and observation.source_id != request.source_id:
                continue
            if (
                request.logical_resource_id is not None
                and observation.logical_resource_id != request.logical_resource_id
            ):
                continue
            if not in_half_open_interval(
                observation.observed_at, request.observed_from, request.observed_to
            ):
                continue
            selected.append(
                ReplaySelection(
                    observation=observation,
                    content=self.content_by_id(observation.raw_content_id),
                )
            )
        return tuple(sorted(selected, key=lambda item: item.observation.observed_at))

    def record_parse(self, result: ParseResult) -> ParseResult:
        existing = self.parse_results.get(result.parse_attempt_id)
        if existing is not None and existing != result:
            raise ValueError("Parse attempt ID collision")
        self.parse_results[result.parse_attempt_id] = result
        return result

    def write_derived(self, record: DerivedRecord) -> tuple[DerivedRecord, bool]:
        identity = (
            record.source_id,
            record.record_type,
            record.natural_key,
            record.parser_version,
            record.payload_sha256,
        )
        existing_id = self.derived_by_identity.get(identity)
        if existing_id is not None:
            return self.derived_records[existing_id], False
        self.derived_by_identity[identity] = record.record_id
        self.derived_records[record.record_id] = record
        return record, True

    def commit_parse(self, result: ParseResult, records: Sequence[DerivedRecord]) -> ParseResult:
        if tuple(record.record_id for record in records) != result.derived_record_ids:
            raise ValueError("Parse result and derived records differ")
        old_records = self.derived_records.copy()
        old_identities = self.derived_by_identity.copy()
        try:
            for record in records:
                stored, _ = self.write_derived(record)
                if stored.record_id != record.record_id:
                    raise ValueError("Derived identity resolved to an unexpected record")
            return self.record_parse(result)
        except Exception:
            self.derived_records = old_records
            self.derived_by_identity = old_identities
            raise

    def record_dead_letter(self, revision: DeadLetterRevision) -> None:
        self.dead_letters.append(revision)

    def open_dead_letters(self, raw_content_id: UUID) -> Sequence[DeadLetterRevision]:
        latest: dict[UUID, DeadLetterRevision] = {}
        for item in self.dead_letters:
            if item.raw_content_id == raw_content_id:
                latest[item.dead_letter_id] = item
        return tuple(item for item in latest.values() if item.status == DeadLetterStatus.OPEN)

    def record_replay_started(
        self, replay_job_id: UUID, request: ReplayRequest, started_at: datetime
    ) -> None:
        existing = self.replay_starts.get(replay_job_id)
        proposed = (request, started_at)
        if existing is not None and existing != proposed:
            raise ValueError("Replay job ID collision")
        self.replay_starts[replay_job_id] = proposed

    def record_replay(self, result: ReplayResult) -> None:
        if result.replay_job_id not in self.replay_starts:
            raise ValueError("Replay job completion has no RUNNING revision")
        existing = self.replay_jobs.get(result.replay_job_id)
        if existing is not None and existing != result:
            raise ValueError("Replay job ID collision")
        self.replay_jobs[result.replay_job_id] = result

    def record_orphan(self, orphan: OrphanObject) -> None:
        self.orphans.setdefault(orphan.object_key, orphan)

    def tracked_object_keys(self) -> set[str]:
        return {content.object_key for content in self.content_records.values()}

    def record_runtime_event(self, event: SourceRuntimeEvent) -> None:
        self.runtime_events.append(event)

    def latest_runtime_event(self, source_id: str) -> SourceRuntimeEvent | None:
        events = [item for item in self.runtime_events if item.source_id == source_id]
        return max(events, key=lambda item: item.recorded_at) if events else None
