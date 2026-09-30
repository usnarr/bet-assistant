"""F15 scheduler: runs the F15.1 job graph and the operations tasks on a cadence.

ADR 0005 records why this small scheduler replaces Prefect. The scheduler gives no
delivery guarantee of its own:

- Each job goes through `JobRunner`. The run key holds the job, the resource, the cutoff
  and the input versions. A restart, a second scheduler or a duplicate tick finds the same
  run and does nothing twice (`ALREADY_SUCCEEDED`). Fenced leases stop a stale worker.
- The cutoff of a job is the start of its current window (`every`). Inside one window the
  job runs once. A job that is not due therefore reports `ALREADY_SUCCEEDED`.
- A job without a handler is `INCOMPLETE`. Its dependants are `BLOCKED`, so publication
  cannot run on incomplete inputs.
- An operations task (alert evaluation, backups) is not a graph job. It runs under a lease
  once per window. Every task must be idempotent.
"""

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from tennis_engine.common.clock import Clock, require_aware
from tennis_engine.common.contracts import Contract, Timestamp
from tennis_engine.monitoring.metrics import MetricsRegistry

from .jobs import ORDER, Capacity, DependencyStatus, JobName, JobOutcome, JobRunner
from .leases import Lease, LeaseStore

logger = logging.getLogger("tennis_engine.operations.scheduler")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def window_start(now: datetime, every: timedelta) -> datetime:
    """The start of the window that holds `now`. Windows are aligned to the Unix epoch."""
    if every <= timedelta(0):
        raise ValueError("A window must be longer than zero")
    elapsed = require_aware(now) - EPOCH
    return EPOCH + every * (elapsed // every)


JobWork = Callable[[datetime, Lease], Mapping[str, str]]


def _no_inputs(cutoff: datetime) -> Mapping[str, str]:
    return {}


@dataclass(frozen=True)
class JobHandler:
    """How the scheduler runs one graph job. `work` gets the cutoff and the fenced lease."""

    every: timedelta
    work: JobWork
    input_versions: Callable[[datetime], Mapping[str, str]] = _no_inputs
    publication_inputs: Callable[[datetime], Mapping[str, DependencyStatus]] | None = None
    capacity: Capacity = Capacity.PROSPECTIVE
    max_attempts: int = 3


@dataclass(frozen=True)
class OperationsTask:
    """A cadence task outside the job graph. `run` gets the window start."""

    name: str
    every: timedelta
    run: Callable[[datetime], Mapping[str, str]]


JobState = Literal[
    "SUCCEEDED", "ALREADY_SUCCEEDED", "BLOCKED", "BUSY", "FAILED", "EXHAUSTED", "NOT_CONFIGURED"
]
TaskState = Literal["SUCCEEDED", "FAILED", "SKIPPED_NOT_DUE", "BUSY"]
COMPLETE_STATES = ("SUCCEEDED", "ALREADY_SUCCEEDED")
FAILED_STATES = ("FAILED", "EXHAUSTED")


class TickReport(Contract):
    started_at: Timestamp
    jobs: dict[str, JobState]
    job_details: dict[str, str]
    tasks: dict[str, TaskState]


def dependency_status(state: JobState) -> DependencyStatus:
    if state in COMPLETE_STATES:
        return DependencyStatus.COMPLETE
    if state in FAILED_STATES:
        return DependencyStatus.FAILED
    return DependencyStatus.INCOMPLETE


class SchedulerMetrics:
    def __init__(self, registry: MetricsRegistry) -> None:
        self.ticks = registry.counter("tennis_scheduler_ticks_total", "Scheduler ticks.")
        self.last_tick = registry.gauge(
            "tennis_scheduler_last_tick_timestamp_seconds",
            "Unix time of the start of the last completed scheduler tick.",
        )
        self.jobs = registry.counter(
            "tennis_job_outcomes_total", "Job outcomes by job and state.", ("job", "state")
        )
        self.job_success = registry.gauge(
            "tennis_job_last_success_timestamp_seconds",
            "Unix time of the tick in which the job was last complete.",
            ("job",),
        )
        self.configured = registry.gauge(
            "tennis_job_configured", "1 when the scheduler has a handler for the job.", ("job",)
        )
        self.tasks = registry.counter(
            "tennis_task_runs_total", "Operations task runs by task and state.", ("task", "state")
        )
        self.task_success = registry.gauge(
            "tennis_task_last_success_timestamp_seconds",
            "Unix time of the last successful run of an operations task.",
            ("task",),
        )


@dataclass
class Scheduler:
    runner: JobRunner
    handlers: Mapping[JobName, JobHandler]
    tasks: Sequence[OperationsTask]
    leases: LeaseStore
    clock: Clock
    metrics: SchedulerMetrics
    owner: str
    task_lease_ttl: timedelta = timedelta(minutes=10)
    _task_windows: dict[str, datetime] = field(default_factory=dict)
    _last_tick: datetime | None = None

    def __post_init__(self) -> None:
        names = [task.name for task in self.tasks]
        if len(names) != len(set(names)):
            raise ValueError("Task names must be unique")
        for job in ORDER:
            self.metrics.configured.set(1 if job in self.handlers else 0, job=job.value)

    def _run_job(
        self, job: JobName, now: datetime, statuses: Mapping[JobName, DependencyStatus]
    ) -> JobOutcome:
        handler = self.handlers[job]
        cutoff = window_start(now, handler.every)

        def work(lease: Lease) -> Mapping[str, str]:
            return handler.work(cutoff, lease)

        return self.runner.run(
            job,
            resource=f"job:{job.value}:{handler.capacity.value.lower()}",
            cutoff=cutoff,
            input_versions=handler.input_versions(cutoff),
            dependencies=statuses,
            work=work,
            capacity=handler.capacity,
            max_attempts=handler.max_attempts,
            publication_inputs=(
                handler.publication_inputs(cutoff) if handler.publication_inputs else None
            ),
        )

    def _run_task(self, task: OperationsTask, now: datetime) -> TaskState:
        window = window_start(now, task.every)
        if self._task_windows.get(task.name) == window:
            return "SKIPPED_NOT_DUE"
        lease = self.leases.acquire(f"task:{task.name}", self.owner, self.task_lease_ttl, now)
        if lease is None:
            return "BUSY"
        try:
            outputs = dict(task.run(window))
        except Exception as error:  # noqa: BLE001 - every failure is recorded
            logger.error(
                "operations task failed",
                extra={"context": {"task": task.name, "error": type(error).__name__}},
            )
            return "FAILED"
        finally:
            self.leases.release(lease)
        self._task_windows[task.name] = window
        self.metrics.task_success.set(self.clock.now().timestamp(), task=task.name)
        logger.info("operations task done", extra={"context": {"task": task.name, **outputs}})
        return "SUCCEEDED"

    def tick(self) -> TickReport:
        now = require_aware(self.clock.now())
        statuses: dict[JobName, DependencyStatus] = {}
        jobs: dict[str, JobState] = {}
        details: dict[str, str] = {}
        for job in ORDER:
            if job not in self.handlers:
                state: JobState = "NOT_CONFIGURED"
            else:
                try:
                    outcome = self._run_job(job, now, statuses)
                    state = outcome.status
                    if outcome.detail:
                        details[job.value] = outcome.detail
                except Exception as error:  # noqa: BLE001 - a store failure fails the job
                    state = "FAILED"
                    details[job.value] = f"{type(error).__name__}: scheduler could not run job"
            statuses[job] = dependency_status(state)
            jobs[job.value] = state
            self.metrics.jobs.inc(job=job.value, state=state)
            if state in COMPLETE_STATES:
                self.metrics.job_success.set(now.timestamp(), job=job.value)
        tasks = {task.name: self._run_task(task, now) for task in self.tasks}
        for name, task_state in tasks.items():
            if task_state != "SKIPPED_NOT_DUE":
                self.metrics.tasks.inc(task=name, state=task_state)
        self.metrics.ticks.inc()
        self.metrics.last_tick.set(now.timestamp())
        self._last_tick = now
        return TickReport(started_at=now, jobs=jobs, job_details=details, tasks=tasks)

    def healthy(self, tick_seconds: float) -> bool:
        """False before the first tick or when the last tick is older than three ticks."""
        if self._last_tick is None:
            return False
        age = (require_aware(self.clock.now()) - self._last_tick).total_seconds()
        return age <= 3 * tick_seconds

    def run(self, stop: threading.Event, tick_seconds: float) -> None:
        while not stop.is_set():
            try:
                report = self.tick()
                logger.info(
                    "scheduler tick",
                    extra={
                        "context": {
                            "jobs": sorted(set(report.jobs.values())),
                            "tasks": report.tasks,
                        }
                    },
                )
            except Exception as error:  # noqa: BLE001 - the loop must survive one bad tick
                logger.error(
                    "scheduler tick failed", extra={"context": {"error": type(error).__name__}}
                )
            stop.wait(tick_seconds)
