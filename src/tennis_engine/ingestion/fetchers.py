"""Governance-gated HTTP/file fetchers with bounded retries and source quotas."""

import asyncio
import email.utils
import random
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from tennis_engine.common.clock import Clock
from tennis_engine.common.ids import new_id
from tennis_engine.governance.contracts import Purpose, Quota, SourcePolicy
from tennis_engine.governance.service import GovernanceService, PermissionDenied

from .contracts import (
    FetchCapture,
    FetchDisposition,
    FetchErrorCode,
    FetchOrigin,
)
from .redaction import request_identity
from .store import IngestionStore, SourceRuntimeEvent

AsyncSleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    maximum_attempts: int = 3
    base_seconds: float = 1.0
    maximum_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not 1 <= self.maximum_attempts <= 10:
            raise ValueError("Retry attempts must be between 1 and 10")
        if self.base_seconds < 0 or self.maximum_seconds < self.base_seconds:
            raise ValueError("Invalid retry delay bounds")

    def delay(self, retry_number: int, jitter: Callable[[], float]) -> float:
        return float(min(self.maximum_seconds, self.base_seconds * (2**retry_number) + jitter()))


@dataclass(frozen=True)
class HttpFetchRequest:
    source_id: str
    logical_resource_id: str
    url: str
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    cached_body: bytes | None = None


class SourceStopped(RuntimeError):
    pass


class SourceLimiter:
    """Process-local hard cap; distributed schedulers must add their own shared lease."""

    def __init__(self, clock: Clock, sleep: AsyncSleep = asyncio.sleep) -> None:
        self.clock = clock
        self.sleep = sleep
        self._semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}
        self._requests: dict[str, deque[datetime]] = defaultdict(deque)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._suspended_until: dict[str, datetime] = {}
        self._access_stopped: set[str] = set()

    def stop_access(self, source_id: str) -> None:
        self._access_stopped.add(source_id)

    def suspend(self, source_id: str, seconds: int) -> None:
        until = self.clock.now() + timedelta(seconds=seconds)
        current = self._suspended_until.get(source_id)
        if current is None or until > current:
            self._suspended_until[source_id] = until

    def status(self, source_id: str) -> str:
        if source_id in self._access_stopped:
            return "ACCESS_STOP"
        until = self._suspended_until.get(source_id)
        if until is not None and self.clock.now() < until:
            return "SUSPENDED"
        return "AVAILABLE"

    async def _account(self, source_id: str, quota: Quota) -> None:
        while True:
            if source_id in self._access_stopped:
                raise SourceStopped(f"Source {source_id} stopped after access control")
            suspended = self._suspended_until.get(source_id)
            now = self.clock.now()
            if suspended is not None and now < suspended:
                await self.sleep((suspended - now).total_seconds())
                continue
            async with self._locks[source_id]:
                now = self.clock.now()
                history = self._requests[source_id]
                cutoff = now - timedelta(seconds=quota.window_seconds)
                while history and history[0] <= cutoff:
                    history.popleft()
                if len(history) < quota.requests:
                    history.append(now)
                    return
                wait = (history[0] + timedelta(seconds=quota.window_seconds) - now).total_seconds()
            await self.sleep(max(wait, 0.001))

    @asynccontextmanager
    async def slot(self, source_id: str, quota: Quota) -> AsyncIterator[None]:
        semaphore = self._semaphores.setdefault(
            (source_id, quota.concurrency), asyncio.Semaphore(quota.concurrency)
        )
        async with semaphore:
            await self._account(source_id, quota)
            yield


class ApprovedHttpFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        governance: GovernanceService,
        clock: Clock,
        *,
        purpose: Purpose,
        limiter: SourceLimiter | None = None,
        retry: RetryPolicy | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        runtime_events: IngestionStore | None = None,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self.client = client
        self.governance = governance
        self.clock = clock
        self.purpose = purpose
        self.sleep = sleep
        self.limiter = limiter or SourceLimiter(clock, sleep)
        self.retry = retry or RetryPolicy()
        self.jitter = jitter
        self.runtime_events = runtime_events
        if not 0 < request_timeout_seconds <= 120:
            raise ValueError("Request timeout must be in (0, 120] seconds")
        self.request_timeout_seconds = request_timeout_seconds

    def _authorize(self, source_id: str) -> SourcePolicy:
        if self.runtime_events is not None:
            event = self.runtime_events.latest_runtime_event(source_id)
            if event is not None and (
                event.event_type == "ACCESS_STOP"
                or (
                    event.event_type == "RATE_LIMITED"
                    and event.until is not None
                    and self.clock.now() < event.until
                )
            ):
                raise SourceStopped(f"Source {source_id} is stopped by persisted runtime state")
        lookup = self.governance.get_source_policy(source_id, self.purpose, self.clock.now())
        if not lookup.decision.allowed or lookup.policy is None:
            raise PermissionDenied(lookup.decision)
        policy = lookup.policy
        if policy.access_method != "rest":
            raise PermissionError("Approved source is not configured for REST access")
        if policy.quota is None:
            raise PermissionError("Approved source has no quota")
        return policy

    async def fetch(self, request: HttpFetchRequest) -> tuple[FetchCapture, ...]:
        captures: list[FetchCapture] = []
        for attempt in range(1, self.retry.maximum_attempts + 1):
            policy = self._authorize(request.source_id)
            assert policy.quota is not None
            requested_at = self.clock.now()
            try:
                async with self.limiter.slot(policy.source_id, policy.quota):
                    response = await self.client.request(
                        request.method,
                        request.url,
                        headers=dict(request.headers),
                        timeout=self.request_timeout_seconds,
                    )
                completed_at = self.clock.now()
                capture = self._from_response(
                    request, response, attempt, requested_at, completed_at
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                capture = FetchCapture(
                    source_id=request.source_id,
                    logical_resource_id=request.logical_resource_id,
                    request_identity=request_identity(request.method, request.url),
                    requested_at=requested_at,
                    completed_at=self.clock.now(),
                    origin=FetchOrigin.REMOTE_HTTP,
                    disposition=FetchDisposition.TRANSIENT_FAILURE,
                    attempt_number=attempt,
                    error_code=(
                        FetchErrorCode.TIMEOUT
                        if isinstance(error, httpx.TimeoutException)
                        else FetchErrorCode.CONNECTION_ERROR
                    ),
                )
            captures.append(capture)
            if capture.disposition == FetchDisposition.ACCESS_STOP:
                self.limiter.stop_access(policy.source_id)
                self._record_runtime_event(capture)
                break
            if capture.disposition == FetchDisposition.RATE_LIMITED:
                self.limiter.suspend(policy.source_id, capture.retry_after_seconds or 60)
                self._record_runtime_event(capture)
                break
            if capture.disposition != FetchDisposition.TRANSIENT_FAILURE:
                break
            if attempt < self.retry.maximum_attempts:
                await self.sleep(self.retry.delay(attempt - 1, self.jitter))
        return tuple(captures)

    def _record_runtime_event(self, capture: FetchCapture) -> None:
        if self.runtime_events is None:
            return
        seconds = capture.retry_after_seconds or 60
        self.runtime_events.record_runtime_event(
            SourceRuntimeEvent(
                event_id=new_id(),
                source_id=capture.source_id,
                event_type=capture.disposition.value,
                until=(
                    capture.completed_at + timedelta(seconds=seconds)
                    if capture.disposition == FetchDisposition.RATE_LIMITED
                    else None
                ),
                details={
                    "status_code": capture.status_code,
                    "error_code": capture.error_code.value if capture.error_code else None,
                    "request_identity": capture.request_identity,
                },
                recorded_at=capture.completed_at,
            )
        )

    def _from_response(
        self,
        request: HttpFetchRequest,
        response: httpx.Response,
        attempt: int,
        requested_at: datetime,
        completed_at: datetime,
    ) -> FetchCapture:
        body = response.content
        body_casefold = body[:65536].lower()
        captcha = b"captcha" in body_casefold
        retry_after = parse_retry_after(response.headers.get("Retry-After"), completed_at)
        common = {
            "source_id": request.source_id,
            "logical_resource_id": request.logical_resource_id,
            "request_identity": request_identity(request.method, request.url),
            "requested_at": requested_at,
            "completed_at": completed_at,
            "origin": FetchOrigin.REMOTE_HTTP,
            "attempt_number": attempt,
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type", "application/octet-stream"),
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "cache_control": response.headers.get("Cache-Control"),
            "provider_request_id": response.headers.get("X-Request-ID"),
            "retry_after_seconds": retry_after,
        }
        if captcha:
            return FetchCapture(
                **common,
                body=body,
                disposition=FetchDisposition.ACCESS_STOP,
                error_code=FetchErrorCode.CAPTCHA,
            )
        if response.status_code in {401, 403}:
            return FetchCapture(
                **common,
                body=body,
                disposition=FetchDisposition.ACCESS_STOP,
                error_code=FetchErrorCode.ACCESS_CONTROL,
            )
        if response.status_code == 429:
            return FetchCapture(
                **common,
                body=body,
                disposition=FetchDisposition.RATE_LIMITED,
                error_code=FetchErrorCode.RATE_LIMITED,
            )
        if response.status_code == 304:
            if request.cached_body is None:
                return FetchCapture(
                    **common,
                    disposition=FetchDisposition.PERMANENT_FAILURE,
                    error_code=FetchErrorCode.HTTP_ERROR,
                )
            return FetchCapture(
                **common,
                body=request.cached_body,
                disposition=FetchDisposition.NOT_MODIFIED,
            )
        if 200 <= response.status_code < 300:
            return FetchCapture(**common, body=body, disposition=FetchDisposition.SUCCESS)
        if 500 <= response.status_code < 600:
            return FetchCapture(
                **common,
                body=body,
                disposition=FetchDisposition.TRANSIENT_FAILURE,
                error_code=FetchErrorCode.HTTP_ERROR,
            )
        return FetchCapture(
            **common,
            body=body,
            disposition=FetchDisposition.PERMANENT_FAILURE,
            error_code=FetchErrorCode.HTTP_ERROR,
        )


class ApprovedFileFetcher:
    def __init__(
        self,
        allowed_root: Path,
        governance: GovernanceService,
        clock: Clock,
        *,
        purpose: Purpose,
    ) -> None:
        self.allowed_root = allowed_root.resolve()
        self.governance = governance
        self.clock = clock
        self.purpose = purpose

    def fetch(
        self,
        path: Path,
        *,
        source_id: str,
        logical_resource_id: str,
        attempt_number: int = 1,
    ) -> FetchCapture:
        decision = self.governance.can_fetch(source_id, self.purpose, self.clock.now())
        if not decision.allowed:
            raise PermissionDenied(decision)
        resolved = path.resolve()
        if resolved != self.allowed_root and self.allowed_root not in resolved.parents:
            raise ValueError("File import must remain within the configured fixture root")
        requested_at = self.clock.now()
        body = resolved.read_bytes()
        return FetchCapture(
            source_id=source_id,
            logical_resource_id=logical_resource_id,
            request_identity=f"FILE {resolved.relative_to(self.allowed_root).as_posix()}",
            requested_at=requested_at,
            completed_at=self.clock.now(),
            origin=FetchOrigin.FILE_IMPORT,
            disposition=FetchDisposition.SUCCESS,
            attempt_number=attempt_number,
            content_type=_file_content_type(resolved),
            body=body,
        )


def _file_content_type(path: Path) -> str:
    return "application/json" if path.suffix.casefold() == ".json" else "application/octet-stream"


def parse_retry_after(value: str | None, now: datetime) -> int | None:
    if value is None:
        return None
    try:
        return max(0, min(86400, int(value)))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0, min(86400, int((parsed.astimezone(UTC) - now).total_seconds())))
