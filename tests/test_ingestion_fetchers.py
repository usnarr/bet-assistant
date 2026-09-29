import asyncio
from datetime import timedelta

import httpx
import pytest
from conftest import source_policy

from tennis_engine.governance.contracts import Purpose
from tennis_engine.governance.service import GovernanceService
from tennis_engine.ingestion.contracts import FetchDisposition, FetchErrorCode
from tennis_engine.ingestion.fetchers import (
    ApprovedFileFetcher,
    ApprovedHttpFetcher,
    HttpFetchRequest,
    RetryPolicy,
    SourceLimiter,
    SourceStopped,
)
from tennis_engine.ingestion.redaction import redact_url, request_identity
from tennis_engine.ingestion.store import MemoryIngestionStore


class ClockAdapter:
    def __init__(self, fixture_clock):
        self.fixture_clock = fixture_clock

    def now(self):
        return self.fixture_clock.now


def approve_http_source(store, *, quota=None):
    policy = source_policy(
        store,
        source_id="synthetic-http",
        access_method="rest",
        quota=quota or {"requests": 100, "window_seconds": 60, "concurrency": 1},
    )
    revision = store.save(policy, expected_revision=0, reason="Synthetic HTTP source")
    assert revision == 2  # Evidence is revision 1 in the append-only fixture journal.
    return policy


def test_http_retries_are_bounded_and_conditional_request_is_preserved(store, clock):
    approve_http_source(store)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, content=b"temporary")
        return httpx.Response(200, json={"ok": True}, headers={"ETag": '"v1"'})

    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.now += timedelta(seconds=seconds)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = ApprovedHttpFetcher(
                client,
                GovernanceService(store),
                ClockAdapter(clock),
                purpose=Purpose.PRODUCTION,
                retry=RetryPolicy(maximum_attempts=3, base_seconds=1, maximum_seconds=5),
                sleep=sleep,
                jitter=lambda: 0,
            )
            return await fetcher.fetch(
                HttpFetchRequest(
                    source_id="synthetic-http",
                    logical_resource_id="event-1",
                    url="https://fixture.invalid/events?api_key=secret&tour=ATP",
                    headers=(("If-None-Match", '"old"'),),
                )
            )

    captures = asyncio.run(run())
    assert [item.disposition for item in captures] == [
        FetchDisposition.TRANSIENT_FAILURE,
        FetchDisposition.SUCCESS,
    ]
    assert len(calls) == 2
    assert calls[0].headers["If-None-Match"] == '"old"'
    assert sleeps == [1]
    assert "secret" not in captures[0].request_identity


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (403, b"denied", FetchErrorCode.ACCESS_CONTROL),
        (200, b"<title>CAPTCHA</title>", FetchErrorCode.CAPTCHA),
    ],
)
def test_access_control_stops_source_without_retry(store, clock, status, body, error):
    approve_http_source(store)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            limiter = SourceLimiter(ClockAdapter(clock))
            runtime = MemoryIngestionStore()
            fetcher = ApprovedHttpFetcher(
                client,
                GovernanceService(store),
                ClockAdapter(clock),
                purpose=Purpose.PRODUCTION,
                limiter=limiter,
                runtime_events=runtime,
            )
            request = HttpFetchRequest(
                source_id="synthetic-http",
                logical_resource_id="event-1",
                url="https://fixture.invalid/events",
            )
            first = await fetcher.fetch(request)
            with pytest.raises(SourceStopped):
                await fetcher.fetch(request)
            return first, limiter, runtime

    captures, limiter, runtime = asyncio.run(run())
    assert len(captures) == calls == 1
    assert captures[0].disposition == FetchDisposition.ACCESS_STOP
    assert captures[0].error_code == error
    assert limiter.status("synthetic-http") == "ACCESS_STOP"
    assert runtime.latest_runtime_event("synthetic-http").event_type == "ACCESS_STOP"


def test_rate_limit_honors_retry_after_by_suspending_schedule(store, clock):
    approve_http_source(store)

    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(429, content=b"slow down", headers={"Retry-After": "17"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            limiter = SourceLimiter(ClockAdapter(clock))
            runtime = MemoryIngestionStore()
            fetcher = ApprovedHttpFetcher(
                client,
                GovernanceService(store),
                ClockAdapter(clock),
                purpose=Purpose.PRODUCTION,
                limiter=limiter,
                runtime_events=runtime,
            )
            captures = await fetcher.fetch(
                HttpFetchRequest(
                    source_id="synthetic-http",
                    logical_resource_id="event-1",
                    url="https://fixture.invalid/events",
                )
            )
            return captures, limiter, runtime

    captures, limiter, runtime = asyncio.run(run())
    assert len(captures) == 1
    assert captures[0].disposition == FetchDisposition.RATE_LIMITED
    assert captures[0].retry_after_seconds == 17
    assert limiter.status("synthetic-http") == "SUSPENDED"
    assert runtime.latest_runtime_event("synthetic-http").until == clock.now + timedelta(seconds=17)


def test_304_requires_and_reuses_archived_body(store, clock):
    approve_http_source(store)

    async def run(cached_body):
        transport = httpx.MockTransport(lambda request: httpx.Response(304))
        async with httpx.AsyncClient(transport=transport) as client:
            return await ApprovedHttpFetcher(
                client,
                GovernanceService(store),
                ClockAdapter(clock),
                purpose=Purpose.PRODUCTION,
            ).fetch(
                HttpFetchRequest(
                    source_id="synthetic-http",
                    logical_resource_id="event-1",
                    url="https://fixture.invalid/events",
                    cached_body=cached_body,
                )
            )

    assert asyncio.run(run(b"previous"))[0].disposition == FetchDisposition.NOT_MODIFIED
    assert asyncio.run(run(None))[0].disposition == FetchDisposition.PERMANENT_FAILURE


def test_file_fetcher_is_approval_gated_and_root_confined(tmp_path, enabled, service, clock):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "sample.json"
    inside.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    fetcher = ApprovedFileFetcher(
        allowed,
        service,
        ClockAdapter(clock),
        purpose=Purpose.PRODUCTION,
    )
    assert (
        fetcher.fetch(
            inside,
            source_id="synthetic-sports",
            logical_resource_id="event-1",
        ).body
        == b"{}"
    )
    with pytest.raises(ValueError, match="configured fixture root"):
        fetcher.fetch(
            outside,
            source_id="synthetic-sports",
            logical_resource_id="event-1",
        )


def test_request_redaction_removes_credentials_fragment_and_userinfo():
    redacted = redact_url("https://user:password@example.test/path?api_key=secret&tour=ATP#private")
    assert redacted == "https://example.test/path?api_key=[REDACTED]&tour=ATP"
    assert request_identity("get", redacted).startswith("GET https://")
