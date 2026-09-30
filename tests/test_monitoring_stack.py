"""F15.3/F15.4 monitoring stack files: generated rules, dashboard, scrape token, secrets."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from tennis_engine.governance.contracts import Role
from tennis_engine.infrastructure import cli as platform_cli
from tennis_engine.monitoring import cadence, instruments
from tennis_engine.monitoring.alerts import Control, load_rules
from tennis_engine.monitoring.dashboard import DATASOURCE, dashboard, expressions, render_dashboard
from tennis_engine.monitoring.metrics import MetricsRegistry
from tennis_engine.monitoring.prometheus import (
    alert_name,
    missing_expression,
    render_rules,
    rule_document,
    threshold_expression,
    to_yaml,
)
from tennis_engine.operations import backups
from tennis_engine.operations import cli as ops_cli
from tennis_engine.operations.scheduler import SchedulerMetrics
from tennis_engine.serving.auth import load_credentials, register_token, token_digest

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs/operations/alert-rules.json"
RULE_FILE = ROOT / "deploy/prometheus/rules/tennis-alerts.yml"
DASHBOARD_FILE = ROOT / "deploy/grafana/dashboards/tennis-operations.json"


def test_committed_prometheus_rules_match_the_generator():
    committed = RULE_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == render_rules(load_rules(RULES))
    assert ops_cli.main(["prometheus-rules", "--check", "--output", str(RULE_FILE)]) == 0


def test_every_rule_has_a_threshold_and_a_missing_telemetry_alert():
    rules = load_rules(RULES)
    group = rule_document(rules)["groups"][0]["rules"]
    assert len(group) == 2 * len(rules.rules)
    by_name = {entry["alert"]: entry for entry in group}
    for rule in rules.rules:
        breach = by_name[alert_name(rule.rule_id)]
        missing = by_name[alert_name(rule.rule_id) + "TelemetryMissing"]
        assert breach["expr"] == threshold_expression(rule)
        assert f"{rule.comparison.value} {rule.threshold}" in breach["expr"]
        assert breach["labels"]["control"] == rule.control.value
        assert breach["labels"]["severity"] == rule.severity.value.lower()
        assert missing["expr"] == missing_expression(rule, rules.max_signal_age_seconds)
        assert f"> {rules.max_signal_age_seconds}" in missing["expr"]
        expected = rule.control if rule.missing_applies_control else Control.NONE
        assert missing["labels"]["control"] == expected.value
        assert breach["labels"]["rule_set"] == missing["labels"]["rule_set"] == rules.version


def test_telemetry_rules_cover_down_absent_and_stale_sources():
    names = {entry["alert"] for entry in rule_document(load_rules(RULES))["groups"][1]["rules"]}
    assert {
        "TennisScrapeTargetDown",
        "TennisApiTargetAbsent",
        "TennisSchedulerTargetAbsent",
        "TennisSchedulerStale",
        "TennisAlertEvaluationStale",
        "TennisSignalExpectationsAbsent",
        "TennisCollectorDown",
        "TennisSignalProducerDown",
    } <= names


def test_yaml_emitter_quotes_scalars_and_nests_lists():
    text = "\n".join(to_yaml({"a": [{"b": 'x"y', "c": 2}], "d": True, "e": []}))
    assert text == 'a:\n  - b: "x\\"y"\n    c: 2\nd: true\ne: []'


def _families() -> set[str]:
    registry = MetricsRegistry()
    instruments.ServingMetrics(registry)
    instruments.AgentMetrics(registry)
    SchedulerMetrics(registry)
    names = set(registry.families)
    names |= {family.name for family in cadence.FAMILIES}
    names |= {family.name for family in (backups.BACKUP_SUCCESS, backups.WAL_ARCHIVE)}
    names.add(backups.WAL_COUNT.name)
    names |= {
        instruments.SOURCE_STATUS.name,
        instruments.SOURCE_AGE.name,
        instruments.RECOMMENDATIONS_ALLOWED.name,
        "tennis_signal_producer_up",
        "tennis_controls_applied_total",
        "tennis_alert_notifications_total",
    }
    return names


PROMETHEUS_BUILTINS = {"up", "ALERTS"}


def _metric_names(expr: str) -> set[str]:
    names = set(re.findall(r"\b(tennis_[a-z0-9_]+|up|ALERTS)\b", expr))
    return {re.sub(r"_(bucket|count|sum)$", "", name) for name in names}


def test_rules_and_dashboard_query_only_exported_metrics():
    known = _families() | PROMETHEUS_BUILTINS
    rule_exprs = [
        entry["expr"]
        for group in rule_document(load_rules(RULES))["groups"]
        for entry in group["rules"]
    ]
    for expr in [*rule_exprs, *expressions()]:
        assert _metric_names(expr) <= known, expr


def test_committed_dashboard_matches_the_generator_and_is_read_only():
    committed = DASHBOARD_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == render_dashboard()
    board = dashboard()
    assert board["editable"] is False
    assert all(panel["datasource"] == DATASOURCE for panel in board["panels"])
    assert len({panel["id"] for panel in board["panels"]}) == len(board["panels"])
    provisioning = (ROOT / "deploy/grafana/provisioning/dashboards/tennis.yml").read_text()
    assert "allowUiUpdates: false" in provisioning and "disableDeletion: true" in provisioning
    source = (ROOT / "deploy/grafana/provisioning/datasources/prometheus.yml").read_text()
    assert f"uid: {DATASOURCE['uid']}" in source and "editable: false" in source
    assert ops_cli.main(["grafana-dashboard", "--check", "--output", str(DASHBOARD_FILE)]) == 0


def test_deploy_files_hold_no_secret_or_local_path():
    for path in (ROOT / "deploy").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8").lower()
            assert "users/" not in text and "users\\" not in text, path.name
            assert "password:" not in text and "credentials:" not in text, path.name


def test_register_token_stores_a_digest_and_is_idempotent(tmp_path):
    path = tmp_path / "api-credentials.json"
    token = "p" * 20 + "rometheus-synthetic-token-00"
    assert register_token(path, "prometheus", Role.OPERATOR, token + "\n") is True
    assert register_token(path, "prometheus", Role.OPERATOR, token) is False
    (stored,) = load_credentials(path)
    assert stored.token_sha256 == token_digest(token)
    assert token not in path.read_text(encoding="utf-8")
    other = "q" * 40
    assert register_token(path, "prometheus", Role.OPERATOR, other) is True
    assert load_credentials(path)[0].token_sha256 == token_digest(other)
    for bad in ("short", "a b" * 20):
        with pytest.raises(ValueError):
            register_token(path, "prometheus", Role.OPERATOR, bad)
    with pytest.raises(ValueError, match="Another identity"):
        register_token(path, "someone-else", Role.OPERATOR, other)


def test_register_command_prints_no_token(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TENNIS_API_CREDENTIALS_FILE", raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("r" * 44 + "\n", encoding="utf-8")
    credentials = tmp_path / "api-credentials.json"
    arguments = ["register-api-token", "--identity", "prometheus", "--role", "operator"]
    arguments += ["--token-file", str(token_file), "--file", str(credentials)]
    assert platform_cli.main(arguments) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"identity": "prometheus", "role": "operator", "changed": True}
    assert "r" * 44 not in output and str(tmp_path) not in output


def test_local_secret_script_creates_files_once(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        "init_local_secrets", ROOT / "scripts/init_local_secrets.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    created = module.create(tmp_path / "secrets")
    assert sorted(created) == sorted(module.SECRETS)
    first = {name: (tmp_path / "secrets" / name).read_text() for name in created}
    assert module.create(tmp_path / "secrets") == []
    assert {name: (tmp_path / "secrets" / name).read_text() for name in created} == first
    assert all(len(value.strip()) >= 32 for value in first.values())
