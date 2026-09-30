"""F15 scheduler: job graph cadence, idempotent ticks, tasks, metrics endpoint (synthetic)."""

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import source_policy

from tennis_engine.common.clock import FrozenClock
from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.governance.service import GovernanceService
from tennis_engine.monitoring.alerts import AlertRule, AlertRuleSet, Control, Severity
from tennis_engine.monitoring.cadence import AlertTask, read_signal_inbox, write_signals
from tennis_engine.monitoring.exporter import AlertNotifications, MetricsServer
from tennis_engine.monitoring.metrics import MetricsRegistry
from tennis_engine.monitoring.signals import Signal
from tennis_engine.operations.jobs import (
    ORDER,
    CapacityLimits,
    CapacityPools,
    DependencyStatus,
    InMemoryJobStore,
    JobName,
    JobRunner,
)
from tennis_engine.operations.leases import InMemoryLeaseStore
from tennis_engine.operations.runtime import fetchable_sources, register_handler
from tennis_engine.operations.scheduler import (
    JobHandler,
    OperationsTask,
    Scheduler,
    SchedulerMetrics,
    window_start,
)

START = datetime(2026, 9, 20, 8, 0, 30, tzinfo=UTC)
FIVE = timedelta(minutes=5)
COMPLETE = DependencyStatus.COMPLETE
ALL_INPUTS = {name: COMPLETE for name in ("identity", "format", "policy", "quote")}


def build(handlers, tasks=(), clock=None, leases=None, store=None, owner="scheduler-a"):
    clock = clock or FrozenClock(START)
    leases = leases or InMemoryLeaseStore()
    store = store or InMemoryJobStore(leases)
    registry = MetricsRegistry()
    runner = JobRunner(store, leases, CapacityPools(CapacityLimits()), clock, owner)
    scheduler = Scheduler(
        runner=runner,
        handlers=handlers,
        tasks=tasks,
        leases=leases,
        clock=clock,
        metrics=SchedulerMetrics(registry),
        owner=owner,
    )
    return scheduler, registry, clock, store, leases


def recording(effects, name):
    def work(cutoff, lease):
        effects.append((name, cutoff))
        return {"output": f"{name}-{cutoff:%H%M}"}

    return work


def all_handlers(effects, inputs=ALL_INPUTS):
    handlers = {job: JobHandler(every=FIVE, work=recording(effects, job.value)) for job in ORDER}
    handlers[JobName.PUBLISH_RECOMMENDATIONS] = JobHandler(
        every=FIVE,
        work=recording(effects, "publish"),
        publication_inputs=lambda cutoff: inputs,
    )
    return handlers


def test_window_start_is_aligned_and_rejects_empty_windows():
    assert window_start(START, FIVE) == datetime(2026, 9, 20, 8, tzinfo=UTC)
    assert window_start(START + timedelta(minutes=4, seconds=29), FIVE) == window_start(START, FIVE)
    assert window_start(START + timedelta(minutes=5), FIVE) > window_start(START, FIVE)
    with pytest.raises(ValueError):
        window_start(START, timedelta(0))
    with pytest.raises(ValueError):
        window_start(datetime(2026, 9, 20), FIVE)


def test_unconfigured_jobs_block_publication_and_its_effect_never_runs():
    effects = []
    handlers = {
        JobName.SYNC_SOURCE_REGISTRY: JobHandler(every=FIVE, work=recording(effects, "sync")),
        JobName.PUBLISH_RECOMMENDATIONS: JobHandler(
            every=FIVE,
            work=recording(effects, "publish"),
            publication_inputs=lambda cutoff: ALL_INPUTS,
        ),
    }
    scheduler, registry, *_ = build(handlers)
    report = scheduler.tick()
    assert report.jobs["sync_source_registry"] == "SUCCEEDED"
    assert report.jobs["evaluate_quotes"] == "NOT_CONFIGURED"
    assert report.jobs["publish_recommendations"] == "BLOCKED"
    assert "DEPENDENCY:evaluate_quotes:INCOMPLETE" in report.job_details["publish_recommendations"]
    assert [name for name, _ in effects] == ["sync"]
    assert registry.value("tennis_job_configured", job="publish_recommendations") == 1
    assert registry.value("tennis_job_configured", job="build_features") == 0
    assert registry.value("tennis_job_outcomes_total", job="build_features", state="NOT_CONFIGURED")


def test_full_graph_runs_once_per_window_and_survives_restart():
    effects = []
    scheduler, _, clock, store, leases = build(all_handlers(effects))
    first = scheduler.tick()
    assert set(first.jobs.values()) == {"SUCCEEDED"}
    # Dependencies run first: the order of effects is the topological order.
    names = [name for name, _ in effects]
    assert names.index("publish") > names.index("evaluate_quotes")
    assert names.index("publish") > names.index("run_data_quality_checks")
    assert len(effects) == len(ORDER)

    # A duplicate tick in the same window does nothing again.
    clock.advance(timedelta(minutes=1))
    assert set(scheduler.tick().jobs.values()) == {"ALREADY_SUCCEEDED"}
    # A restarted scheduler (new process, same stores, other owner) also does nothing.
    restarted, *_ = build(
        all_handlers(effects), clock=clock, leases=leases, store=store, owner="scheduler-b"
    )
    assert set(restarted.tick().jobs.values()) == {"ALREADY_SUCCEEDED"}
    assert len(effects) == len(ORDER)

    # The next window has a new cutoff, so every job runs once more.
    clock.advance(FIVE)
    assert set(scheduler.tick().jobs.values()) == {"SUCCEEDED"}
    assert len(effects) == 2 * len(ORDER)


def test_incomplete_publication_input_blocks_publication():
    effects = []
    inputs = ALL_INPUTS | {"identity": DependencyStatus.INCOMPLETE}
    scheduler, *_ = build(all_handlers(effects, inputs))
    report = scheduler.tick()
    assert report.jobs["evaluate_quotes"] == "SUCCEEDED"
    assert report.jobs["publish_recommendations"] == "BLOCKED"
    assert "INPUT:identity:INCOMPLETE" in report.job_details["publish_recommendations"]
    assert "publish" not in [name for name, _ in effects]


def test_a_failed_job_blocks_its_dependants_and_is_retried():
    effects = []
    handlers = all_handlers(effects)
    calls = []

    def broken(cutoff, lease):
        calls.append(cutoff)
        if len(calls) == 1:
            raise RuntimeError("synthetic outage with secret-looking text")
        return {"fixtures": "v1"}

    handlers[JobName.SYNC_FIXTURES] = JobHandler(every=FIVE, work=broken)
    scheduler, registry, clock, *_ = build(handlers)
    report = scheduler.tick()
    assert report.jobs["sync_fixtures"] == "FAILED"
    assert report.jobs["resolve_entities"] == "BLOCKED"
    assert "DEPENDENCY:sync_fixtures:FAILED" in report.job_details["resolve_entities"]
    assert report.jobs["publish_recommendations"] == "BLOCKED"
    assert "secret-looking" not in json.dumps(report.model_dump(mode="json"))
    assert registry.value("tennis_job_outcomes_total", job="sync_fixtures", state="FAILED") == 1
    clock.advance(timedelta(minutes=1))
    retried = scheduler.tick()
    assert retried.jobs["sync_fixtures"] == "SUCCEEDED"
    assert retried.jobs["publish_recommendations"] == "SUCCEEDED"


def test_a_store_failure_marks_the_job_failed_without_stopping_the_tick():
    class BrokenStore(InMemoryJobStore):
        def register(self, run):
            raise ConnectionError("database down")

    leases = InMemoryLeaseStore()
    effects = []
    scheduler, *_ = build(all_handlers(effects), leases=leases, store=BrokenStore(leases))
    report = scheduler.tick()
    assert set(report.jobs.values()) == {"FAILED"}
    assert report.job_details["sync_source_registry"].startswith("ConnectionError")
    assert effects == []


def test_tasks_run_once_per_window_retry_after_failure_and_respect_leases():
    calls = []

    def flaky(window):
        calls.append(window)
        if len(calls) == 1:
            raise OSError("synthetic")
        return {"done": "yes"}

    task = OperationsTask("backup", FIVE, flaky)
    scheduler, registry, clock, _, leases = build({}, tasks=(task,))
    assert scheduler.tick().tasks == {"backup": "FAILED"}
    clock.advance(timedelta(minutes=1))
    assert scheduler.tick().tasks == {"backup": "SUCCEEDED"}
    clock.advance(timedelta(minutes=1))
    assert scheduler.tick().tasks == {"backup": "SKIPPED_NOT_DUE"}
    assert registry.value("tennis_task_runs_total", task="backup", state="FAILED") == 1
    assert registry.value("tennis_task_runs_total", task="backup", state="SUCCEEDED") == 1
    assert registry.value("tennis_task_last_success_timestamp_seconds", task="backup")

    # Another scheduler holds the task lease: this one reports BUSY and runs nothing.
    clock.advance(FIVE)
    leases.acquire("task:backup", "scheduler-b", timedelta(minutes=10), clock.now())
    before = len(calls)
    assert scheduler.tick().tasks == {"backup": "BUSY"}
    assert len(calls) == before

    with pytest.raises(ValueError):
        build({}, tasks=(task, task))


def test_health_needs_a_recent_tick():
    scheduler, registry, clock, *_ = build({})
    assert not scheduler.healthy(60)
    scheduler.tick()
    assert scheduler.healthy(60)
    assert registry.value("tennis_scheduler_last_tick_timestamp_seconds") == START.timestamp()
    clock.advance(timedelta(minutes=4))
    assert not scheduler.healthy(60)


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def _post(url, body: bytes, length=None):
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if length is not None:
        request.add_header("Content-Length", str(length))
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_metrics_server_serves_metrics_health_and_the_alert_webhook():
    registry = MetricsRegistry()
    healthy = [False]
    server = MetricsServer(
        registry,
        "127.0.0.1",
        0,
        health=lambda: healthy[0],
        alert_sink=AlertNotifications(registry),
    )
    server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        status, body = _get(base + "/metrics")
        assert status == 200 and "tennis_alert_notifications_total" in body
        assert _get(base + "/health")[0] == 503
        healthy[0] = True
        assert _get(base + "/health")[0] == 200
        assert _get(base + "/other")[0] == 404
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "TennisSignalMissing", "severity": "critical"},
                    "annotations": {"summary": "free text that must not be logged"},
                }
            ],
        }
        assert _post(base + "/alertmanager", json.dumps(payload).encode()) == 200
        assert (
            registry.value(
                "tennis_alert_notifications_total",
                alertname="TennisSignalMissing",
                severity="critical",
                status="firing",
            )
            == 1
        )
        assert _post(base + "/alertmanager", b"[1, 2]") == 400
        assert _post(base + "/alertmanager", b"{not json") == 400
        assert _post(base + "/alertmanager", json.dumps({"x": 1}).encode()) == 400
        assert _post(base + "/metrics", b"{}") == 404
    finally:
        server.stop()


def test_unstarted_server_stops_without_blocking():
    MetricsServer(MetricsRegistry(), "127.0.0.1", 0).stop()


def _rules():
    return AlertRuleSet(
        version="scheduler-test-v1",
        status="PROPOSED",
        max_signal_age_seconds=900,
        rules=(
            AlertRule(
                rule_id="zero-events",
                signal="parser_events",
                scope="source",
                comparison="<=",
                threshold=Decimal(0),
                severity=Severity.CRITICAL,
                control=Control.SOURCE_STOP,
                description="Synthetic rule.",
            ),
            AlertRule(
                rule_id="mismatch",
                signal="settlement_mismatch_count",
                scope="global",
                comparison=">",
                threshold=Decimal(0),
                severity=Severity.CRITICAL,
                control=Control.GLOBAL_STOP,
                missing_applies_control=False,
                description="Synthetic rule.",
            ),
        ),
    )


def test_alert_task_exports_one_snapshot_and_applies_controls():
    clock = FrozenClock(START)
    registry = MetricsRegistry()
    applied = []
    values = {"events": Decimal(3)}

    def parser(now):
        return (
            Signal(name="parser_events", scope="book-a", value=values["events"], observed_at=now),
        )

    def broken(now):
        raise ConnectionError("ledger store down")

    task = AlertTask(
        rules=_rules(),
        producers={"parser": parser, "ledger": broken},
        expected_sources=lambda now: ("book-a", "book-b"),
        apply=lambda alerts: applied.append(alerts) or (),
        clock=clock,
        registry=registry,
    )
    result = task(START)
    assert result["alerts"] == "2"  # book-b parser and the global ledger signal are missing
    text = registry.render()
    assert 'tennis_signal_value{signal="parser_events",scope="book-a"} 3' in text
    assert 'tennis_signal_expected{signal="parser_events",scope="book-b"} 1' in text
    assert 'tennis_signal_expected{signal="settlement_mismatch_count",scope="global"} 1' in text
    assert 'tennis_signal_value{signal="settlement_mismatch_count"' not in text
    assert 'reason="TELEMETRY_MISSING",scope="book-b"' in text
    assert 'tennis_signal_producer_up{producer="ledger"} 0' in text
    assert 'tennis_signal_producer_up{producer="parser"} 1' in text
    assert len(applied) == 1

    # A new run replaces the snapshot: the old alert samples do not linger.
    values["events"] = Decimal(0)
    task(START)
    text = registry.render()
    assert 'tennis_signal_value{signal="parser_events",scope="book-a"} 0' in text
    assert 'rule_id="zero-events",severity="CRITICAL",reason="THRESHOLD",scope="book-a"' in text


def test_signal_inbox_reads_valid_files_and_counts_invalid_ones(tmp_path):
    good = Signal(name="leakage_test_failures", value=Decimal(0), observed_at=START)
    write_signals(tmp_path / "leakage.json", [good])
    (tmp_path / "bad.json").write_text('[{"name": "x"}]', encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("not a signal file", encoding="utf-8")
    signals, invalid = read_signal_inbox(tmp_path)
    assert signals == (good,) and invalid == 1
    assert read_signal_inbox(tmp_path / "missing") == ((), 0)


def test_register_job_reads_only_fetchable_sources(store, clock):
    governance = GovernanceService(store)
    store.save(
        source_policy(store, "approved-source"),
        expected_revision=0,
        reason="SYS-01 synthetic approval",
    )
    draft = source_policy(store, "draft-source", state="DRAFT", kill_switch=True)
    store.save(draft, expected_revision=0, reason="draft")
    assert fetchable_sources(governance, clock()) == ("approved-source",)
    store.set_source_stop("approved-source", True, reason="synthetic stop")
    assert fetchable_sources(governance, clock()) == ()

    handler = register_handler(store_path(store), FIVE)
    outputs = handler.work(clock(), None)
    assert outputs["fetchable_sources"] == "0"
    assert int(outputs["journal_revision"]) >= 3


def store_path(store):
    row = store.db.execute("PRAGMA database_list").fetchone()
    return Path(row["file"])


def test_scheduler_principals_are_scoped():
    from tennis_engine.operations import runtime

    assert runtime.REGISTER_READER == Principal(identity="scheduler", role=Role.DASHBOARD)
    assert runtime.CONTROL_PRINCIPAL.role == Role.OPERATOR
