"""F15.3/F15.4 metrics, signals, alert rules and deterministic controls (synthetic)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import source_policy
from fastapi.testclient import TestClient
from pydantic import ValidationError
from serving_support import (
    BOOK_SOURCE,
    READ_AT,
    TOKENS,
    FakeChecks,
    bet,
    build,
    headers,
    stored,
    watch,
)
from test_foundation_api import Probe
from test_pricing_decision import fresh
from test_pricing_publication import observation

from tennis_engine.governance.contracts import Principal, Purpose, Role
from tennis_engine.governance.service import GovernanceService
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.ingestion.bookmakers.adapter import SnapshotMetrics
from tennis_engine.monitoring.alerts import (
    AlertRule,
    AlertRuleSet,
    Control,
    Severity,
    evaluate,
    load_rules,
)
from tennis_engine.monitoring.controls import apply_controls
from tennis_engine.monitoring.metrics import MetricsRegistry, Sample, label_value
from tennis_engine.monitoring.signals import (
    Signal,
    missing_rate,
    parser_signals,
    population_stability_index,
)
from tennis_engine.operations import cli as ops_cli
from tennis_engine.serving.api import create_app
from tennis_engine.serving.checks import GovernanceReadChecks, per_thread

RULES = Path("configs/operations/alert-rules.json")
D = Decimal


def metrics(client, role=Role.OPERATOR):
    response = client.get("/metrics", headers=headers(role))
    assert response.status_code == 200, response.text
    return response.text


def snapshot(**overrides):
    values = {
        "bookmaker": "synthetic-book",
        "parser_version": "synthetic-parser-v1",
        "events": 10,
        "markets": 10,
        "selections": 20,
        "rejected_records": 0,
        "supported_selections": 20,
        "unknown_market_labels": 0,
        "null_start_events": 0,
        "duplicate_selection_ids": 0,
    }
    return SnapshotMetrics.model_validate(values | overrides)


# Metrics registry ----------------------------------------------------------------------


def test_registry_renders_prometheus_text_and_sanitizes_labels():
    registry = MetricsRegistry()
    counter = registry.counter("tennis_things_total", "Things.", ("kind",))
    counter.inc(kind='bad "value"\nwith?query=secret token')
    counter.inc(2, kind="ok")
    with pytest.raises(ValueError):
        counter.inc(-1, kind="ok")
    with pytest.raises(ValueError):
        counter.inc(other="x")
    histogram = registry.histogram("tennis_latency_seconds", "Latency.", buckets=(0.1, 1.0))
    histogram.observe(0.05)
    histogram.observe(0.5)
    text = registry.render()
    assert "# TYPE tennis_things_total counter" in text
    assert 'tennis_things_total{kind="ok"} 2' in text
    assert '"' not in label_value('a"b') and "\n" not in label_value("a\nb")
    assert "?" not in text.split("tennis_things_total{kind=")[1].split("}")[0]
    assert 'tennis_latency_seconds_bucket{le="0.1"} 1' in text
    assert 'tennis_latency_seconds_bucket{le="+Inf"} 2' in text
    assert "tennis_latency_seconds_count 2" in text


def test_a_failed_collector_is_reported_and_not_hidden():
    registry = MetricsRegistry()
    gauge = registry.gauge("tennis_source_fresh", "Fresh.", ("source_id",))

    def broken():
        raise RuntimeError("store down")

    registry.register_collector("broken", broken, (gauge.family,))
    registry.register_collector(
        "working", lambda: [Sample("tennis_source_fresh", 1, (("source_id", "s"),))], ()
    )
    text = registry.render()
    assert 'tennis_metrics_collector_up{collector="broken"} 0' in text
    # The working collector declared no family, so its sample is refused.
    assert 'tennis_metrics_collector_up{collector="working"} 0' in text
    assert "tennis_source_fresh{" not in text


# API metrics -----------------------------------------------------------------------------


def test_metrics_need_an_internal_role_and_hold_no_token_or_raw_ids():
    record = bet()
    client, *_ = build([stored(record), stored(watch())])
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=headers(Role.DASHBOARD)).status_code == 403
    assert client.get("/metrics", headers=headers(Role.AGENT)).status_code == 403
    client.get(f"/v1/audit/recommendations/{record.decision_id}", headers=headers())
    client.get("/v1/tennis/recommendations", headers=headers())
    text = metrics(client)
    assert 'route="/v1/audit/recommendations/{recommendation_id}"' in text
    assert str(record.decision_id) not in text
    assert all(token not in text for token in TOKENS.values())
    assert 'tennis_rechecked_records_total{recorded="BET",served="BET"} 1' in text
    assert 'tennis_source_status{source_id="synthetic-book",status="OK"} 1' in text
    assert 'tennis_source_observation_age_seconds{source_id="synthetic-book"} 15' in text
    # The sports source has no quote observation: no age sample, status UNKNOWN.
    assert 'tennis_source_observation_age_seconds{source_id="synthetic-sports"}' not in text
    assert 'tennis_source_status{source_id="synthetic-sports",status="UNKNOWN"} 1' in text
    assert 'tennis_recommendations_allowed{account_scope="shadow"} 1' in text
    assert metrics(client, Role.POLICY_REVIEWER)


def test_read_time_blocks_are_counted_by_reason_code():
    checks = FakeChecks()
    client, *_ = build([stored(bet())], checks=checks)
    checks.disabled[BOOK_SOURCE] = "SOURCE_DISABLED"
    client.get("/v1/tennis/recommendations", headers=headers())
    text = metrics(client)
    assert 'tennis_read_time_blocks_total{reason="SOURCE:SOURCE_DISABLED"} 1' in text
    assert 'tennis_rechecked_records_total{recorded="BET",served="NO_BET"} 1' in text
    assert 'tennis_source_status{source_id="synthetic-book",status="DISABLED"} 1' in text


def test_metrics_are_off_without_serving():
    client = TestClient(create_app(Settings(environment="test"), Probe(True)))
    assert client.get("/metrics", headers=headers()).status_code == 503


# Signals ---------------------------------------------------------------------------------


def test_psi_missing_rate_and_parser_signals():
    base = [D("0.3"), D("0.5"), D("0.7")] * 30
    assert population_stability_index(base, base) == D("0")
    shifted = [D("0.9"), D("0.95")] * 45
    psi = population_stability_index(base, shifted)
    assert psi is not None and psi > D("0.25")
    assert population_stability_index([], base) is None
    assert missing_rate([True, False, False, True]) == D("0.5")
    assert missing_rate([]) is None
    values = {
        s.name: s.value
        for s in parser_signals("synthetic-book", snapshot(events=4), snapshot(), READ_AT)
    }
    assert values["parser_events"] == 4 and values["parser_volume_change"] == D("0.6")
    first = parser_signals("synthetic-book", snapshot(), None, READ_AT)
    assert "parser_volume_change" not in {s.name for s in first}


# Alert rules -----------------------------------------------------------------------------


def signal(name, value, scope="global", at=READ_AT):
    return Signal(name=name, scope=scope, value=value, observed_at=at)


def healthy(source="synthetic-book"):
    return [
        *parser_signals(source, snapshot(), snapshot(), READ_AT),
        signal("source_observation_age_seconds", D(10), source),
        *(
            signal(name, D(0))
            for name in (
                "stale_publication_count",
                "settlement_mismatch_count",
                "leakage_test_failures",
                "payout_inconsistency_count",
                "identity_review_queue_size",
                "feature_missing_rate",
                "model_probability_psi",
            )
        ),
    ]


def test_proposed_rules_load_and_healthy_signals_raise_nothing():
    rules = load_rules(RULES)
    assert rules.status == "PROPOSED"
    assert evaluate(rules, healthy(), sources=["synthetic-book"], now=READ_AT) == ()


def test_breach_missing_and_stale_signals_raise_alerts():
    rules = load_rules(RULES)
    broken = [s for s in healthy() if s.name != "parser_events"]
    broken.append(signal("parser_events", D(0), "synthetic-book"))
    alerts = evaluate(rules, broken, sources=["synthetic-book"], now=READ_AT)
    assert [(a.rule_id, a.reason, a.control) for a in alerts] == [
        ("parser-zero-events", "THRESHOLD", Control.SOURCE_STOP)
    ]
    # An expected source without signals is missing telemetry, never healthy.
    missing = evaluate(rules, healthy(), sources=["synthetic-book", "other-book"], now=READ_AT)
    assert {a.scope for a in missing} == {"other-book"}
    assert {a.reason for a in missing} == {"TELEMETRY_MISSING"}
    assert Control.SOURCE_STOP in {a.control for a in missing}
    # A signal older than the rule-set limit is missing too.
    later = READ_AT + timedelta(seconds=rules.max_signal_age_seconds + 1)
    stale = evaluate(rules, healthy(), sources=["synthetic-book"], now=later)
    assert stale and {a.reason for a in stale} == {"TELEMETRY_MISSING"}
    # Missing global evidence raises the alert but does not stop by itself.
    partial = [s for s in healthy() if s.name != "leakage_test_failures"]
    leakage = evaluate(rules, partial, sources=["synthetic-book"], now=READ_AT)
    assert [(a.rule_id, a.control) for a in leakage] == [("future-leakage", Control.NONE)]


def test_rule_validation_limits_controls():
    base = {
        "rule_id": "x",
        "signal": "parser_events",
        "scope": "global",
        "comparison": "<=",
        "threshold": "0",
        "description": "synthetic",
    }
    with pytest.raises(ValidationError, match="CRITICAL"):
        AlertRule.model_validate(base | {"severity": "WARNING", "control": "GLOBAL_STOP"})
    with pytest.raises(ValidationError, match="source-scoped"):
        AlertRule.model_validate(base | {"severity": "CRITICAL", "control": "SOURCE_STOP"})
    rule = AlertRule.model_validate(base | {"severity": "CRITICAL"})
    with pytest.raises(ValidationError, match="unique"):
        AlertRuleSet(version="v", status="PROPOSED", max_signal_age_seconds=60, rules=(rule, rule))
    assert rule.severity == Severity.CRITICAL


# Deterministic controls on the real F01 journal ------------------------------------------


@pytest.fixture
def operator(tmp_path, store, enabled, clock):
    store.save(source_policy(store, BOOK_SOURCE), expected_revision=0, reason="Synthetic book")
    principal = Principal(identity="fixture-operator", role=Role.OPERATOR)
    instance = GovernanceStore(tmp_path / "governance.sqlite3", principal, clock)
    yield instance
    instance.close()


def test_critical_source_alert_stops_the_source_and_f14_serves_no_bet(
    operator, store, tmp_path, clock
):
    reader = Principal(identity="fixture-dashboard", role=Role.DASHBOARD)
    checks = GovernanceReadChecks(
        per_thread(
            lambda: GovernanceService(
                GovernanceStore(tmp_path / "governance.sqlite3", reader, clock, read_only=True)
            )
        ),
        lambda item, now: fresh(observation=observation()),
    )
    client, *_ = build([stored(bet())], checks=checks)
    row = client.get("/v1/tennis/recommendations", headers=headers()).json()["recommendations"][0]
    assert row["actionable"] is True, row["read_time_reasons"]

    rules = load_rules(RULES)
    now = clock()
    signals = [s.model_copy(update={"observed_at": now}) for s in healthy(BOOK_SOURCE)]
    signals = [s for s in signals if s.name != "parser_events"]
    signals.append(signal("parser_events", D(0), BOOK_SOURCE, now))
    alerts = evaluate(rules, signals, sources=[BOOK_SOURCE], now=now)
    actions = apply_controls(alerts, operator)
    assert [(a.control, a.scope, a.outcome) for a in actions] == [
        (Control.SOURCE_STOP, BOOK_SOURCE, "APPLIED")
    ]
    denied = GovernanceService(operator).can_fetch(BOOK_SOURCE, Purpose.PROTOTYPE, now)
    assert denied.reason.value == "SOURCE_STOPPED"
    row = client.get("/v1/tennis/recommendations", headers=headers()).json()["recommendations"][0]
    assert row["decision"] == "NO_BET"
    assert "SOURCE:synthetic-book:SOURCE_STOPPED" in row["read_time_reasons"]

    # Idempotent: the same alerts append nothing.
    revisions = len(operator.export()["records"])
    again = apply_controls(alerts, operator)
    assert [a.outcome for a in again] == ["ALREADY_STOPPED"]
    assert len(operator.export()["records"]) == revisions
    # An operator cannot resume; only a reviewer can.
    with pytest.raises(PermissionError):
        operator.set_source_stop(BOOK_SOURCE, False, reason="Operator resume attempt")
    store.set_source_stop(BOOK_SOURCE, False, reason="Reviewed root cause; resume")
    assert GovernanceService(store).can_fetch(BOOK_SOURCE, Purpose.PROTOTYPE, now).allowed


def test_critical_global_alert_turns_the_global_stop_on(operator, clock):
    rules = load_rules(RULES)
    now = clock()
    signals = [s.model_copy(update={"observed_at": now}) for s in healthy(BOOK_SOURCE)]
    signals = [s for s in signals if s.name != "settlement_mismatch_count"]
    signals.append(signal("settlement_mismatch_count", D(1), at=now))
    alerts = evaluate(rules, signals, sources=[BOOK_SOURCE], now=now)
    assert [a.rule_id for a in alerts] == ["settlement-mismatch"]
    assert operator.global_disabled(now) is False
    actions = apply_controls(alerts, operator)
    assert actions[0].control == Control.GLOBAL_STOP and actions[0].outcome == "APPLIED"
    assert operator.global_disabled(now) is True
    # Warnings never apply a control.
    warning = evaluate(rules, [signal("model_probability_psi", D(1), at=now)], sources=[], now=now)
    assert apply_controls([a for a in warning if a.severity == "WARNING"], operator) == ()


def test_ops_cli_evaluates_and_applies_controls(tmp_path, operator, monkeypatch, capsys, clock):
    monkeypatch.setattr(ops_cli, "datetime", _FrozenDatetime(clock()))
    monkeypatch.setattr("tennis_engine.governance.cli.getpass.getuser", lambda: "ops-user")
    access = tmp_path / "access.json"
    access.write_text('{"ops-user": "operator"}')
    signals = [s.model_copy(update={"observed_at": clock()}) for s in healthy(BOOK_SOURCE)]
    path = tmp_path / "signals.json"
    path.write_text(json.dumps([s.model_dump(mode="json") for s in signals]))
    args = ["evaluate-alerts", "--signals", str(path), "--sources", BOOK_SOURCE]
    assert ops_cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["alerts"] == []

    signals.append(signal("stale_publication_count", D(2), at=clock() + timedelta(seconds=1)))
    path.write_text(json.dumps([s.model_dump(mode="json") for s in signals]))
    database = ["--database", str(tmp_path / "governance.sqlite3"), "--access-file", str(access)]
    assert ops_cli.main([*args, "--apply", *database]) == 1
    result = json.loads(capsys.readouterr().out)
    assert [a["rule_id"] for a in result["alerts"]] == ["stale-publication"]
    assert result["actions"][0]["outcome"] == "APPLIED"
    # The CLI writes with the system clock, so read the state at the system time.
    assert operator.global_disabled(datetime.now(UTC)) is True
    path.write_text('[{"name": "x", "value": 1.5, "observed_at": "2026-09-20T12:00:00+00:00"}]')
    assert ops_cli.main(args) == 2
    assert "1.5" not in capsys.readouterr().out


def _FrozenDatetime(instant):  # noqa: N802 - stands in for the datetime class
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant + timedelta(seconds=2)

    return Frozen
