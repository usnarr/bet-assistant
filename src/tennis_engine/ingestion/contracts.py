"""Strict F03 contracts for fetch evidence, parsing, and replay."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp


class FetchOrigin(StrEnum):
    REMOTE_HTTP = "REMOTE_HTTP"
    FILE_IMPORT = "FILE_IMPORT"
    REPLAY_CACHE = "REPLAY_CACHE"


class FetchDisposition(StrEnum):
    SUCCESS = "SUCCESS"
    NOT_MODIFIED = "NOT_MODIFIED"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    ACCESS_STOP = "ACCESS_STOP"
    RATE_LIMITED = "RATE_LIMITED"
    STORAGE_FAILURE = "STORAGE_FAILURE"


class FetchErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    ACCESS_CONTROL = "ACCESS_CONTROL"
    CAPTCHA = "CAPTCHA"
    RATE_LIMITED = "RATE_LIMITED"
    STORAGE_ERROR = "STORAGE_ERROR"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"


class ArchiveState(StrEnum):
    PENDING = "PENDING"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


class ParseStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DRY_RUN = "DRY_RUN"


class DeadLetterStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    ABANDONED = "ABANDONED"


class FetchCapture(Contract):
    """One source attempt before its optional body is archived."""

    source_id: Identifier
    logical_resource_id: Annotated[str, Field(min_length=1, max_length=512)]
    request_identity: Annotated[str, Field(min_length=1, max_length=2048)]
    requested_at: Timestamp
    completed_at: Timestamp
    origin: FetchOrigin
    disposition: FetchDisposition
    attempt_number: Annotated[int, Field(ge=1, le=20, strict=True)]
    status_code: Annotated[int, Field(ge=100, le=599, strict=True)] | None = None
    content_type: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    body: bytes | None = Field(default=None, repr=False)
    etag: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    last_modified: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    cache_control: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    provider_request_id: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    retry_after_seconds: Annotated[int, Field(ge=0, le=86400, strict=True)] | None = None
    error_code: FetchErrorCode | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.completed_at < self.requested_at:
            raise ValueError("Fetch completion cannot precede its request")
        succeeded = self.disposition in {
            FetchDisposition.SUCCESS,
            FetchDisposition.NOT_MODIFIED,
        }
        if succeeded and self.body is None:
            raise ValueError("A successful capture requires bytes, including a resolved 304 body")
        if succeeded and self.error_code is not None:
            raise ValueError("A successful capture cannot carry an error")
        if not succeeded and self.error_code is None:
            raise ValueError("A failed capture requires a typed error")
        return self

    @property
    def refreshes_observation(self) -> bool:
        return self.origin != FetchOrigin.REPLAY_CACHE and self.disposition in {
            FetchDisposition.SUCCESS,
            FetchDisposition.NOT_MODIFIED,
        }


class FetchResult(Contract):
    """Durable, secret-free evidence for one archived fetch attempt."""

    schema_version: Literal["1.0"] = "1.0"
    attempt_id: UUID
    command_id: UUID
    source_id: Identifier
    logical_resource_id: str
    request_identity: str
    requested_at: Timestamp
    completed_at: Timestamp
    origin: FetchOrigin
    disposition: FetchDisposition
    attempt_number: int
    status_code: int | None = None
    content_type: str | None = None
    body_sha256: Digest | None = None
    raw_content_id: UUID | None = None
    object_key: str | None = None
    observation_id: UUID | None = None
    etag: str | None = None
    last_modified: str | None = None
    cache_control: str | None = None
    provider_request_id: str | None = None
    retry_after_seconds: int | None = None
    parser_candidate: Identifier
    error_code: FetchErrorCode | None = None
    policy_version: Identifier
    policy_revision: Annotated[int, Field(ge=1, strict=True)]

    @model_validator(mode="after")
    def references_are_complete(self) -> Self:
        raw = (self.body_sha256, self.raw_content_id, self.object_key)
        if any(item is None for item in raw) and any(item is not None for item in raw):
            raise ValueError("Raw content hash, ID and object key must be present together")
        if self.observation_id is not None and self.raw_content_id is None:
            raise ValueError("An observation requires archived raw content")
        return self


class ParsedItem(Contract):
    record_type: Identifier
    natural_key: Annotated[str, Field(min_length=1, max_length=512)]
    payload: dict[str, Any]


class ParseResult(Contract):
    parse_attempt_id: UUID
    observation_id: UUID
    parser_version: Identifier
    status: ParseStatus
    derived_record_ids: tuple[UUID, ...] = ()
    errors: tuple[str, ...] = ()


class ReplayRequest(Contract):
    source_id: Identifier | None = None
    logical_resource_id: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    observed_from: Timestamp | None = None
    observed_to: Timestamp | None = None
    parser_version: Identifier
    dry_run: bool = False

    @model_validator(mode="after")
    def interval_is_ordered(self) -> Self:
        if (
            self.observed_from is not None
            and self.observed_to is not None
            and self.observed_to <= self.observed_from
        ):
            raise ValueError("Replay interval must be [from, to)")
        return self


class ReplayResult(Contract):
    replay_job_id: UUID
    request: ReplayRequest
    started_at: Timestamp
    completed_at: Timestamp
    selected_observations: Annotated[int, Field(ge=0)]
    accepted: Annotated[int, Field(ge=0)]
    rejected: Annotated[int, Field(ge=0)]
    derived_records: Annotated[int, Field(ge=0)]


class ObservationRecord(Contract):
    observation_id: UUID
    attempt_id: UUID
    command_id: UUID
    source_id: Identifier
    logical_resource_id: str
    raw_content_id: UUID
    observed_at: Timestamp
    effective_at: Timestamp | None = None


class ContentRecord(Contract):
    content_id: UUID
    source_id: Identifier
    body_sha256: Digest
    object_key: str
    content_type: str
    size_bytes: Annotated[int, Field(ge=0)]
    state: ArchiveState
    created_at: Timestamp


class ReplaySelection(Contract):
    observation: ObservationRecord
    content: ContentRecord


def in_half_open_interval(value: datetime, lower: datetime | None, upper: datetime | None) -> bool:
    return (lower is None or value >= lower) and (upper is None or value < upper)
