"""Production wiring for the F15 scheduler (`tennis-ops scheduler`).

The scheduler process holds:

- The F15.1 job graph on PostgreSQL job and lease stores. Only jobs with a handler run.
  In this repository only `sync_source_registry` has a handler, so every job that needs
  collected data stays `INCOMPLETE` and publication stays `BLOCKED`.
- The alert task: signal producers, the versioned rule set and the deterministic
  controls on the F01 journal (operator principal; a control only stops).
- A metrics endpoint for Prometheus and an Alertmanager webhook.
"""

import logging
import os
import signal
import socket
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import FrameType

from minio import Minio
from sqlalchemy import Engine

from tennis_engine.common.clock import Clock, SystemClock
from tennis_engine.governance.contracts import Principal, Purpose, Role
from tennis_engine.governance.service import GovernanceService
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.database import build_engine
from tennis_engine.infrastructure.object_store import ImmutableObjectStore, S3ObjectStore
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.monitoring.alerts import Alert, load_rules
from tennis_engine.monitoring.cadence import AlertTask, read_signal_inbox
from tennis_engine.monitoring.controls import ControlAction, apply_controls
from tennis_engine.monitoring.exporter import AlertNotifications, MetricsServer
from tennis_engine.monitoring.metrics import MetricsRegistry
from tennis_engine.monitoring.signals import Signal, freshness_signals

from .backups import (
    DEFAULT_RECOVERY_CONFIG,
    backup_objects,
    journal_snapshots,
    load_recovery_config,
    register_backup_collector,
    snapshot_journal,
    wal_archiver,
)
from .jobs import CapacityLimits, CapacityPools, JobName, JobRunner
from .leases import Lease, PostgresLeaseStore
from .postgres import PostgresJobStore
from .recovery import reconcile_ledgers
from .scheduler import JobHandler, OperationsTask, Scheduler, SchedulerMetrics

logger = logging.getLogger("tennis_engine.operations.runtime")

DEFAULT_RULES = Path("configs/operations/alert-rules.json")
# The scheduler reads the register as a viewer and stops sources as an operator.
REGISTER_READER = Principal(identity="scheduler", role=Role.DASHBOARD)
CONTROL_PRINCIPAL = Principal(identity="scheduler", role=Role.OPERATOR)
FETCH_PURPOSES = (Purpose.PROTOTYPE, Purpose.PRODUCTION)


@dataclass(frozen=True)
class SchedulerOptions:
    tick_seconds: float = 60.0
    metrics_host: str = "0.0.0.0"  # noqa: S104 - inside the container, internal network only
    metrics_port: int = 9101
    rules: Path = DEFAULT_RULES
    signal_inbox: Path = Path("var/signals")
    alert_every: timedelta = timedelta(minutes=1)
    register_every: timedelta = timedelta(minutes=5)
    apply_controls: bool = True
    extra_tasks: Sequence[OperationsTask] = field(default_factory=tuple)
    # F15.7 backups. None turns the object and journal backups off.
    backup_root: Path | None = None
    # The PostgreSQL backup volume, read-only, for the base backup recovery point.
    postgres_backup_root: Path | None = None
    recovery_config: Path = DEFAULT_RECOVERY_CONFIG


def backup_tasks(
    settings: Settings,
    options: SchedulerOptions,
    journal: Path,
    objects: ImmutableObjectStore,
    clock: Clock,
) -> tuple[OperationsTask, ...]:
    """Object and journal backups on the agreed schedule (RPO 15 min)."""
    if options.backup_root is None:
        return ()
    config = load_recovery_config(options.recovery_config)
    object_dir = options.backup_root / "objects"
    journal_dir = options.backup_root / "journal"
    keep = int(config.retention.get("journal_snapshots_kept", 2016))

    def objects_task(window: datetime) -> dict[str, str]:
        report = backup_objects(objects, object_dir, clock.now())
        if report.problems:
            raise RuntimeError(f"object backup problems: {len(report.problems)}")
        return {"listed": str(report.listed), "copied": str(report.copied)}

    def journal_task(window: datetime) -> dict[str, str]:
        snapshot_journal(journal, journal_dir, window, keep)
        return {"snapshots": str(len(journal_snapshots(journal_dir)))}

    return (
        OperationsTask(
            "backup_objects",
            timedelta(seconds=config.schedule.object_backup_interval_seconds),
            objects_task,
        ),
        OperationsTask(
            "backup_journal",
            timedelta(seconds=config.schedule.journal_backup_interval_seconds),
            journal_task,
        ),
    )


def s3_objects(settings: Settings) -> ImmutableObjectStore:
    client = Minio(
        settings.object_store_endpoint,
        access_key=settings.object_store_access_key.get_secret_value(),
        secret_key=settings.object_store_secret_key.get_secret_value(),
        secure=settings.object_store_secure,
    )
    return S3ObjectStore(client, settings.object_store_bucket)


def fetchable_sources(governance: GovernanceService, now: datetime) -> tuple[str, ...]:
    """Source IDs in the F01 register that may be fetched now for some purpose."""
    keys = sorted({row["entity_key"] for row in governance.store.records("source")})
    return tuple(
        key
        for key in keys
        if any(governance.can_fetch(key, purpose, now).allowed for purpose in FETCH_PURPOSES)
    )


def _read_register(journal: Path, now: datetime) -> tuple[int, tuple[str, ...]]:
    store = GovernanceStore(journal, REGISTER_READER, read_only=True)
    try:
        revision = store.db.execute("SELECT coalesce(max(revision), 0) FROM journal").fetchone()
        return int(revision[0]), fetchable_sources(GovernanceService(store), now)
    finally:
        store.close()


def register_handler(journal: Path, every: timedelta) -> JobHandler:
    """`sync_source_registry`: read the F01 register at the cutoff. It changes nothing."""

    def work(cutoff: datetime, lease: Lease) -> dict[str, str]:
        revision, sources = _read_register(journal, cutoff)
        return {"journal_revision": str(revision), "fetchable_sources": str(len(sources))}

    return JobHandler(every=every, work=work)


def _apply(journal: Path) -> Callable[[Sequence[Alert]], tuple[ControlAction, ...]]:
    def apply(alerts: Sequence[Alert]) -> tuple[ControlAction, ...]:
        store = GovernanceStore(journal, CONTROL_PRINCIPAL)
        try:
            return apply_controls(alerts, store)
        finally:
            store.close()

    return apply


def ledger_signal(engine: Engine, now: datetime) -> tuple[Signal, ...]:
    check = reconcile_ledgers(engine, now)
    return (
        Signal(
            name="settlement_mismatch_count",
            value=Decimal(len(check.unbalanced)),
            observed_at=now,
        ),
    )


@dataclass
class SchedulerProcess:
    scheduler: Scheduler
    server: MetricsServer
    registry: MetricsRegistry
    engine: Engine
    options: SchedulerOptions

    def close(self) -> None:
        self.server.stop()
        self.engine.dispose()


def build_scheduler(
    settings: Settings,
    options: SchedulerOptions,
    *,
    clock: Clock | None = None,
    engine: Engine | None = None,
    objects: ImmutableObjectStore | None = None,
) -> SchedulerProcess:
    from tennis_engine.serving.wiring import build_recommendation_service

    if settings.governance_journal is None:
        raise ValueError("The scheduler needs TENNIS_GOVERNANCE_JOURNAL")
    journal = settings.governance_journal
    clock = clock or SystemClock()
    engine = engine or build_engine(settings.database_url, connect_timeout=5)
    registry = MetricsRegistry()
    leases = PostgresLeaseStore(engine)
    owner = f"scheduler:{socket.gethostname()}:{os.getpid()}"
    runner = JobRunner(
        PostgresJobStore(engine, leases), leases, CapacityPools(CapacityLimits()), clock, owner
    )
    service = build_recommendation_service(settings, engine=engine, clock=clock)
    inbox_invalid = registry.gauge(
        "tennis_signal_inbox_invalid_files", "Signal inbox files that failed validation."
    )

    def inbox(now: datetime) -> tuple[Signal, ...]:
        signals, invalid = read_signal_inbox(options.signal_inbox)
        inbox_invalid.set(invalid)
        return signals

    alert_task = AlertTask(
        rules=load_rules(options.rules),
        producers={
            "source_health": lambda now: freshness_signals(service.source_health(now), now),
            "ledger": lambda now: ledger_signal(engine, now),
            "inbox": inbox,
        },
        expected_sources=lambda now: _read_register(journal, now)[1],
        apply=_apply(journal) if options.apply_controls else None,
        clock=clock,
        registry=registry,
    )
    backups: tuple[OperationsTask, ...] = ()
    if options.backup_root is not None:
        backups = backup_tasks(settings, options, journal, objects or s3_objects(settings), clock)
        register_backup_collector(
            registry,
            postgres_base=(
                options.postgres_backup_root / "base" if options.postgres_backup_root else None
            ),
            objects=options.backup_root / "objects",
            journal=options.backup_root / "journal",
            archiver=lambda: wal_archiver(engine),
        )
    scheduler = Scheduler(
        runner=runner,
        handlers={JobName.SYNC_SOURCE_REGISTRY: register_handler(journal, options.register_every)},
        tasks=(OperationsTask("evaluate_alerts", options.alert_every, alert_task),)
        + backups
        + tuple(options.extra_tasks),
        leases=leases,
        clock=clock,
        metrics=SchedulerMetrics(registry),
        owner=owner,
    )
    server = MetricsServer(
        registry,
        options.metrics_host,
        options.metrics_port,
        health=lambda: scheduler.healthy(options.tick_seconds),
        alert_sink=AlertNotifications(registry),
    )
    return SchedulerProcess(scheduler, server, registry, engine, options)


def run_scheduler(process: SchedulerProcess) -> None:
    """Run until SIGTERM or SIGINT. A stop ends the loop after the current tick."""
    stop = threading.Event()

    def request_stop(number: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    process.server.start()
    logger.info(
        "scheduler started",
        extra={"context": {"tick_seconds": process.options.tick_seconds}},
    )
    try:
        process.scheduler.run(stop, process.options.tick_seconds)
    finally:
        process.close()
