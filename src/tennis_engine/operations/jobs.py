"""F15.1 job graph, dependency and publication gates, capacity classes and job records.

The scheduler is not trusted for exactly-once delivery. Idempotency comes from the
job-run key (job, scope, cutoff and input versions), one success per run, and a fenced
lease around the effect. Backfills and replays use their own capacity pool, so they
cannot take the slots of prospective collection.
"""

import hashlib
import json
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field

from tennis_engine.common.clock import Clock, require_aware
from tennis_engine.common.contracts import Contract, Digest, Timestamp
from tennis_engine.common.ids import stable_id

from .leases import Lease, LeaseLost, LeaseStore, require_current


class JobName(StrEnum):
    """Blueprint section 33.1."""

    SYNC_SOURCE_REGISTRY = "sync_source_registry"
    SYNC_RULES_DOCUMENTS = "sync_rules_documents"
    SYNC_PLAYER_PROFILES = "sync_player_profiles"
    SYNC_RANKINGS = "sync_rankings"
    SYNC_TOURNAMENTS = "sync_tournaments"
    SYNC_FIXTURES = "sync_fixtures"
    SYNC_RESULTS = "sync_results"
    SYNC_MATCH_STATS = "sync_match_stats"
    SYNC_WEATHER_FORECASTS = "sync_weather_forecasts"
    DISCOVER_BOOKMAKER_EVENTS = "discover_bookmaker_events"
    POLL_BOOKMAKER_EVENTS = "poll_bookmaker_events"
    RESOLVE_ENTITIES = "resolve_entities"
    NORMALIZE_MARKETS = "normalize_markets"
    BUILD_FEATURES = "build_features"
    SCORE_UPCOMING_MATCHES = "score_upcoming_matches"
    EVALUATE_QUOTES = "evaluate_quotes"
    PUBLISH_RECOMMENDATIONS = "publish_recommendations"
    SETTLE_COMPLETED_BETS = "settle_completed_bets"
    RUN_DATA_QUALITY_CHECKS = "run_data_quality_checks"
    RUN_DRIFT_CHECKS = "run_drift_checks"


J = JobName
_SYNC = frozenset({J.SYNC_SOURCE_REGISTRY})
# Blueprint section 33.2, plus the source registry before every external collection.
DEPENDENCIES: dict[JobName, frozenset[JobName]] = {
    J.SYNC_SOURCE_REGISTRY: frozenset(),
    J.SYNC_RULES_DOCUMENTS: _SYNC,
    J.SYNC_PLAYER_PROFILES: _SYNC,
    J.SYNC_RANKINGS: _SYNC,
    J.SYNC_TOURNAMENTS: _SYNC,
    J.SYNC_FIXTURES: _SYNC,
    J.SYNC_RESULTS: _SYNC,
    J.SYNC_MATCH_STATS: _SYNC,
    J.RESOLVE_ENTITIES: frozenset({J.SYNC_PLAYER_PROFILES, J.SYNC_FIXTURES}),
    J.SYNC_WEATHER_FORECASTS: frozenset({J.SYNC_FIXTURES, J.RESOLVE_ENTITIES, J.SYNC_TOURNAMENTS}),
    J.DISCOVER_BOOKMAKER_EVENTS: _SYNC,
    J.POLL_BOOKMAKER_EVENTS: frozenset({J.DISCOVER_BOOKMAKER_EVENTS}),
    J.NORMALIZE_MARKETS: frozenset({J.POLL_BOOKMAKER_EVENTS, J.RESOLVE_ENTITIES}),
    J.BUILD_FEATURES: frozenset(
        {
            J.SYNC_FIXTURES,
            J.RESOLVE_ENTITIES,
            J.SYNC_TOURNAMENTS,
            J.SYNC_WEATHER_FORECASTS,
            J.SYNC_RANKINGS,
            J.SYNC_RESULTS,
            J.SYNC_MATCH_STATS,
        }
    ),
    J.SCORE_UPCOMING_MATCHES: frozenset({J.BUILD_FEATURES}),
    J.EVALUATE_QUOTES: frozenset(
        {J.SCORE_UPCOMING_MATCHES, J.NORMALIZE_MARKETS, J.SYNC_RULES_DOCUMENTS}
    ),
    J.PUBLISH_RECOMMENDATIONS: frozenset({J.EVALUATE_QUOTES, J.RUN_DATA_QUALITY_CHECKS}),
    J.SETTLE_COMPLETED_BETS: frozenset({J.SYNC_RESULTS, J.SYNC_RULES_DOCUMENTS}),
    J.RUN_DATA_QUALITY_CHECKS: frozenset({J.NORMALIZE_MARKETS, J.RESOLVE_ENTITIES}),
    J.RUN_DRIFT_CHECKS: frozenset({J.SCORE_UPCOMING_MATCHES}),
}


def topological_order(graph: Mapping[JobName, frozenset[JobName]]) -> tuple[JobName, ...]:
    """Raise ValueError on a cycle or an unknown dependency."""
    order: list[JobName] = []
    state: dict[JobName, str] = {}

    def visit(job: JobName) -> None:
        if state.get(job) == "done":
            return
        if state.get(job) == "open":
            raise ValueError(f"The job graph has a cycle at {job}")
        if job not in graph:
            raise ValueError(f"Unknown job {job}")
        state[job] = "open"
        for dependency in sorted(graph[job]):
            visit(dependency)
        state[job] = "done"
        order.append(job)

    for job in sorted(graph):
        visit(job)
    return tuple(order)


ORDER = topological_order(DEPENDENCIES)


class DependencyStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"


# Inputs that publication needs complete (F15.1). Unknown means incomplete.
PUBLICATION_INPUTS = ("identity", "format", "policy", "quote")


def dependency_blockers(
    job: JobName, statuses: Mapping[JobName, DependencyStatus]
) -> tuple[str, ...]:
    return tuple(
        f"DEPENDENCY:{dependency.value}:{statuses.get(dependency, DependencyStatus.INCOMPLETE)}"
        for dependency in sorted(DEPENDENCIES[job])
        if statuses.get(dependency) != DependencyStatus.COMPLETE
    )


def publication_blockers(inputs: Mapping[str, DependencyStatus]) -> tuple[str, ...]:
    """Identity, format, policy and quote inputs must all be complete before publication."""
    return tuple(
        f"INPUT:{name}:{inputs.get(name, DependencyStatus.INCOMPLETE)}"
        for name in PUBLICATION_INPUTS
        if inputs.get(name) != DependencyStatus.COMPLETE
    )


class Capacity(StrEnum):
    PROSPECTIVE = "PROSPECTIVE"
    BACKFILL = "BACKFILL"


class CapacityLimits(Contract):
    """Proposed per-process concurrency. Tune with source quotas before production."""

    prospective: Annotated[int, Field(ge=1, le=64, strict=True)] = 4
    backfill: Annotated[int, Field(ge=1, le=64, strict=True)] = 1


class CapacityPools:
    """Separate semaphores, so a backfill can never use a prospective slot."""

    def __init__(self, limits: CapacityLimits) -> None:
        self._pools = {
            Capacity.PROSPECTIVE: threading.BoundedSemaphore(limits.prospective),
            Capacity.BACKFILL: threading.BoundedSemaphore(limits.backfill),
        }

    @contextmanager
    def slot(self, capacity: Capacity) -> Iterator[bool]:
        """Yield True with a slot, or False at once when the pool is full."""
        pool = self._pools[capacity]
        acquired = pool.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                pool.release()


class JobRun(Contract):
    job_run_id: UUID
    idempotency_key: Digest
    job: JobName
    capacity: Capacity
    resource: str
    cutoff: Timestamp
    input_versions: dict[str, str]
    max_attempts: Annotated[int, Field(ge=1, le=20, strict=True)]
    created_at: Timestamp


AttemptStatus = Literal["BLOCKED", "RUNNING", "SUCCEEDED", "FAILED"]


class JobAttempt(Contract):
    job_run_id: UUID
    sequence: Annotated[int, Field(ge=1, strict=True)]
    status: AttemptStatus
    lease_owner: str | None = None
    fencing_token: int | None = None
    dependency_status: dict[str, str] = Field(default_factory=dict)
    output_versions: dict[str, str] = Field(default_factory=dict)
    detail: str = ""
    recorded_at: Timestamp


def idempotency_key(
    job: JobName, resource: str, cutoff: datetime, input_versions: Mapping[str, str]
) -> str:
    body = json.dumps(
        {
            "job": job.value,
            "resource": resource,
            "cutoff": require_aware(cutoff).isoformat(),
            "inputs": dict(sorted(input_versions.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


class JobStore(Protocol):
    def register(self, run: JobRun) -> JobRun:
        """Insert the run, or return the existing run with the same idempotency key."""
        ...

    def attempts(self, job_run_id: UUID) -> tuple[JobAttempt, ...]: ...

    def append(self, attempt: JobAttempt, lease: Lease | None, now: datetime) -> None:
        """Append one attempt. A RUNNING or SUCCEEDED attempt needs the current lease."""
        ...


class InMemoryJobStore:
    def __init__(self, leases: LeaseStore) -> None:
        self.leases = leases
        self._runs: dict[str, JobRun] = {}
        self._attempts: dict[UUID, list[JobAttempt]] = {}
        self._lock = threading.Lock()

    def register(self, run: JobRun) -> JobRun:
        with self._lock:
            return self._runs.setdefault(run.idempotency_key, run)

    def attempts(self, job_run_id: UUID) -> tuple[JobAttempt, ...]:
        return tuple(self._attempts.get(job_run_id, ()))

    def append(self, attempt: JobAttempt, lease: Lease | None, now: datetime) -> None:
        with self._lock:
            existing = self._attempts.setdefault(attempt.job_run_id, [])
            # The fence check comes first, as in PostgreSQL where it locks the lease row.
            if attempt.status in ("RUNNING", "SUCCEEDED"):
                if lease is None:
                    raise LeaseLost("A fenced attempt needs a lease")
                require_current(lease, self.leases.current(lease.resource), now)
            _check_append(existing, attempt)
            existing.append(attempt)


def _check_append(existing: list[JobAttempt], attempt: JobAttempt) -> None:
    if attempt.sequence != len(existing) + 1:
        raise ValueError("Attempts are appended in sequence")
    if attempt.status == "SUCCEEDED" and any(item.status == "SUCCEEDED" for item in existing):
        raise ValueError("A job run has at most one success")


class JobOutcome(Contract):
    run: JobRun
    status: Literal["SUCCEEDED", "ALREADY_SUCCEEDED", "BLOCKED", "BUSY", "FAILED", "EXHAUSTED"]
    attempt: JobAttempt | None
    detail: str = ""


Work = Callable[[Lease], Mapping[str, str]]


class JobRunner:
    """Runs one job under the dependency gate, a capacity slot and a fenced lease."""

    def __init__(
        self,
        store: JobStore,
        leases: LeaseStore,
        pools: CapacityPools,
        clock: Clock,
        owner: str,
        lease_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        self.store = store
        self.leases = leases
        self.pools = pools
        self.clock = clock
        self.owner = owner
        self.lease_ttl = lease_ttl

    def _attempt(self, run: JobRun, status: AttemptStatus, **values: object) -> JobAttempt:
        sequence = len(self.store.attempts(run.job_run_id)) + 1
        return JobAttempt.model_validate(
            {
                "job_run_id": run.job_run_id,
                "sequence": sequence,
                "status": status,
                "recorded_at": self.clock.now(),
            }
            | values
        )

    def run(
        self,
        job: JobName,
        *,
        resource: str,
        cutoff: datetime,
        input_versions: Mapping[str, str],
        dependencies: Mapping[JobName, DependencyStatus],
        work: Work,
        capacity: Capacity = Capacity.PROSPECTIVE,
        max_attempts: int = 3,
        publication_inputs: Mapping[str, DependencyStatus] | None = None,
    ) -> JobOutcome:
        key = idempotency_key(job, resource, cutoff, input_versions)
        run = self.store.register(
            JobRun(
                job_run_id=stable_id("job-run", key),
                idempotency_key=key,
                job=job,
                capacity=capacity,
                resource=resource,
                cutoff=cutoff,
                input_versions=dict(input_versions),
                max_attempts=max_attempts,
                created_at=self.clock.now(),
            )
        )
        history = self.store.attempts(run.job_run_id)
        done = next((item for item in history if item.status == "SUCCEEDED"), None)
        if done is not None:
            # Duplicate delivery: the effect happened once; do nothing.
            return JobOutcome(run=run, status="ALREADY_SUCCEEDED", attempt=done)
        if sum(item.status == "FAILED" for item in history) >= run.max_attempts:
            return JobOutcome(run=run, status="EXHAUSTED", attempt=history[-1])
        blockers = dependency_blockers(job, dependencies)
        if job == JobName.PUBLISH_RECOMMENDATIONS:
            blockers += publication_blockers(publication_inputs or {})
        status_map = {name.value: value.value for name, value in dependencies.items()}
        if blockers:
            attempt = self._attempt(
                run, "BLOCKED", dependency_status=status_map, detail=";".join(blockers)
            )
            self.store.append(attempt, None, self.clock.now())
            return JobOutcome(run=run, status="BLOCKED", attempt=attempt, detail=attempt.detail)
        with self.pools.slot(capacity) as has_slot:
            if not has_slot:
                return JobOutcome(run=run, status="BUSY", attempt=None, detail="CAPACITY_FULL")
            lease = self.leases.acquire(resource, self.owner, self.lease_ttl, self.clock.now())
            if lease is None:
                return JobOutcome(run=run, status="BUSY", attempt=None, detail="LEASE_HELD")
            fenced = {"lease_owner": self.owner, "fencing_token": lease.fencing_token}
            self.store.append(
                self._attempt(run, "RUNNING", dependency_status=status_map, **fenced),
                lease,
                self.clock.now(),
            )
            try:
                outputs = dict(work(lease))
            except Exception as error:  # noqa: BLE001 - every failure is recorded
                attempt = self._attempt(
                    run, "FAILED", detail=f"{type(error).__name__}: job failed", **fenced
                )
                self.store.append(attempt, None, self.clock.now())
                self.leases.release(lease)
                return JobOutcome(run=run, status="FAILED", attempt=attempt, detail=attempt.detail)
            attempt = self._attempt(run, "SUCCEEDED", output_versions=outputs, **fenced)
            try:
                # The fence check and the success record are one step.
                self.store.append(attempt, lease, self.clock.now())
            except LeaseLost as error:
                failed = self._attempt(run, "FAILED", detail=f"LEASE_LOST: {error}", **fenced)
                self.store.append(failed, None, self.clock.now())
                return JobOutcome(run=run, status="FAILED", attempt=failed, detail=failed.detail)
            self.leases.release(lease)
            return JobOutcome(run=run, status="SUCCEEDED", attempt=attempt)
