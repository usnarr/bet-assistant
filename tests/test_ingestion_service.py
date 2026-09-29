import gzip
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tennis_engine.common.clock import FrozenClock
from tennis_engine.common.ids import stable_id
from tennis_engine.infrastructure.object_store import LocalObjectStore
from tennis_engine.ingestion.contracts import (
    ArchiveState,
    ContentRecord,
    FetchCapture,
    FetchDisposition,
    FetchErrorCode,
    FetchOrigin,
    ParsedItem,
    ReplayRequest,
)
from tennis_engine.ingestion.parser import ParserRegistry
from tennis_engine.ingestion.service import ArchiveError, IngestionService, ParseRejected
from tennis_engine.ingestion.store import MemoryIngestionStore
from tennis_engine.ingestion.synthetic import SyntheticSportsParser

NOW = datetime(2026, 9, 19, 10, tzinfo=UTC)


def capture(body: bytes | None, *, key: str, origin=FetchOrigin.FILE_IMPORT, success=True):
    return FetchCapture(
        source_id="synthetic-sports",
        logical_resource_id="synthetic-event-1",
        request_identity="FILE payload.json",
        requested_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        origin=origin,
        disposition=FetchDisposition.SUCCESS if success else FetchDisposition.TRANSIENT_FAILURE,
        attempt_number=1,
        status_code=200 if success else None,
        content_type="application/json" if body is not None else None,
        body=body,
        error_code=None if success else FetchErrorCode.TIMEOUT,
    )


def archive(service, item, key):
    return service.archive(
        idempotency_key=key,
        observation_window=NOW,
        capture=item,
        parser_candidate="synthetic-sports-v1",
        policy_version="fixture-v1",
        policy_revision=1,
    )


def test_identical_responses_share_content_but_refresh_observation(tmp_path):
    repository = MemoryIngestionStore()
    service = IngestionService(repository, LocalObjectStore(tmp_path / "objects"), FrozenClock(NOW))
    body = (tmp_path.parent / "missing").name.encode()

    first = archive(service, capture(body, key="one"), "one")
    second = archive(service, capture(body, key="two"), "two")

    assert first.raw_content_id == second.raw_content_id
    assert first.observation_id != second.observation_id
    assert len(repository.content_records) == 1
    assert len(repository.observations) == 2


def test_failure_and_cached_read_do_not_refresh_observation(tmp_path):
    repository = MemoryIngestionStore()
    service = IngestionService(repository, LocalObjectStore(tmp_path / "objects"), FrozenClock(NOW))
    failed = archive(service, capture(b"timeout response", key="failed", success=False), "failed")
    cached = archive(
        service,
        capture(b"cached", key="cached", origin=FetchOrigin.REPLAY_CACHE),
        "cached",
    )

    assert failed.observation_id is None
    assert cached.observation_id is None
    assert repository.observations == {}


def test_retry_batch_preserves_every_attempt_but_only_success_is_observed(tmp_path):
    repository = MemoryIngestionStore()
    service = IngestionService(repository, LocalObjectStore(tmp_path / "objects"), FrozenClock(NOW))
    first = capture(b"temporary", key="retry", success=False)
    second = capture(b"success", key="retry").model_copy(update={"attempt_number": 2})
    results = service.archive_attempts(
        idempotency_key="retry-command",
        observation_window=NOW,
        captures=(first, second),
        parser_candidate="synthetic-sports-v1",
        policy_version="fixture-v1",
        policy_revision=1,
    )
    assert len(results) == len(repository.fetches) == 2
    assert results[0].observation_id is None
    assert results[1].observation_id is not None
    assert len(repository.observations) == 1


def test_replay_is_idempotent_and_parser_versions_are_distinct(tmp_path):
    fixture = (
        Path(__file__).parent / "fixtures" / "sources" / "synthetic-sports-v1" / "payload.json"
    ).read_bytes()
    repository = MemoryIngestionStore()
    service = IngestionService(repository, LocalObjectStore(tmp_path / "objects"), FrozenClock(NOW))
    archived = archive(service, capture(fixture, key="event"), "event")
    assert archived.observation_id is not None
    registry = ParserRegistry((SyntheticSportsParser(),))

    first = service.replay(ReplayRequest(parser_version="synthetic-sports-v1"), registry)
    second = service.replay(ReplayRequest(parser_version="synthetic-sports-v1"), registry)

    assert first.derived_records == second.derived_records == 1
    assert len(repository.derived_records) == 1
    assert len(repository.parse_results) == 2

    class V2Parser:
        version = "synthetic-sports-v2"

        def parse(self, body):
            original = SyntheticSportsParser().parse(body)[0]
            return (
                ParsedItem(
                    record_type=original.record_type,
                    natural_key=original.natural_key,
                    payload=original.payload | {"parser_marker": "v2"},
                ),
            )

    registry.register(V2Parser())
    service.replay(ReplayRequest(parser_version="synthetic-sports-v2"), registry)
    assert {item.parser_version for item in repository.derived_records.values()} == {
        "synthetic-sports-v1",
        "synthetic-sports-v2",
    }


def test_schema_drift_is_dead_lettered_and_dry_run_does_not_write(tmp_path):
    repository = MemoryIngestionStore()
    service = IngestionService(repository, LocalObjectStore(tmp_path / "objects"), FrozenClock(NOW))
    malformed = b'{"schema_version":"changed","events":[]}'
    bad = archive(service, capture(malformed, key="bad"), "bad")
    assert bad.observation_id is not None
    with pytest.raises(ParseRejected):
        service.parse_observation(
            repository.observation(bad.observation_id), SyntheticSportsParser()
        )
    assert repository.dead_letters[-1].status.value == "OPEN"

    class ReviewedRecoveryParser:
        version = "synthetic-sports-recovery-v2"

        def parse(self, body):
            return (
                ParsedItem(
                    record_type="synthetic-source-event",
                    natural_key="recovered-event",
                    payload={"reviewed_recovery": True},
                ),
            )

    service.replay(
        ReplayRequest(parser_version=ReviewedRecoveryParser.version),
        ParserRegistry((ReviewedRecoveryParser(),)),
    )
    assert repository.dead_letters[-1].status.value == "RESOLVED"

    good_body = (
        Path(__file__).parent / "fixtures" / "sources" / "synthetic-sports-v1" / "payload.json"
    ).read_bytes()
    good = archive(service, capture(good_body, key="good"), "good")
    assert good.observation_id is not None
    result = service.replay(
        ReplayRequest(parser_version="synthetic-sports-v1", dry_run=True),
        ParserRegistry((SyntheticSportsParser(),)),
    )
    assert result.accepted == 1
    assert result.rejected == 1
    assert len(repository.derived_records) == 1  # The earlier recovery; dry-run added nothing.


def test_corrupt_archive_blocks_parsing_and_reconciliation_records_orphans(tmp_path):
    repository = MemoryIngestionStore()
    objects = LocalObjectStore(tmp_path / "objects")
    service = IngestionService(repository, objects, FrozenClock(NOW))
    fixture = (
        Path(__file__).parent / "fixtures" / "sources" / "synthetic-sports-v1" / "payload.json"
    ).read_bytes()
    archived = archive(service, capture(fixture, key="event"), "event")
    assert archived.observation_id is not None and archived.object_key is not None
    objects._path(archived.object_key).write_bytes(gzip.compress(b"corrupt", mtime=0))
    with pytest.raises(ParseRejected, match="hash"):
        service.parse_observation(
            repository.observation(archived.observation_id), SyntheticSportsParser()
        )
    objects.put("raw/orphan/aa/deadbeef.bin.gz", gzip.compress(b"orphan", mtime=0))
    report = service.reconcile()
    assert report == {"recovered": 0, "failed": 1, "orphans": 1}


class FailingObjectStore(LocalObjectStore):
    def put(self, key, content):
        raise OSError("synthetic storage outage")


def test_storage_failure_keeps_pending_evidence_and_blocks_checkpoint(tmp_path):
    repository = MemoryIngestionStore()
    service = IngestionService(
        repository, FailingObjectStore(tmp_path / "objects"), FrozenClock(NOW)
    )
    with pytest.raises(ArchiveError, match="checkpoint"):
        archive(service, capture(b"payload", key="event"), "event")
    assert next(iter(repository.content_records.values())).state == ArchiveState.FAILED
    assert repository.dead_letters


def test_reconciliation_recovers_bytes_written_after_staging(tmp_path):
    repository = MemoryIngestionStore()
    objects = LocalObjectStore(tmp_path / "objects")
    service = IngestionService(repository, objects, FrozenClock(NOW))
    raw = b"interrupted"
    sha = hashlib.sha256(raw).hexdigest()
    content = repository.stage_content(
        ContentRecord(
            content_id=stable_id("raw-content", f"synthetic-sports:{sha}"),
            source_id="synthetic-sports",
            body_sha256=sha,
            object_key=f"raw/synthetic-sports/{sha[:2]}/{sha}.bin.gz",
            content_type="application/octet-stream",
            size_bytes=len(raw),
            state=ArchiveState.PENDING,
            created_at=NOW,
        )
    )
    objects.put(content.object_key, gzip.compress(raw, mtime=0))
    assert service.reconcile() == {"recovered": 1, "failed": 0, "orphans": 0}
    assert repository.content_by_id(content.content_id).state == ArchiveState.ARCHIVED
