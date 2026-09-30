"""F15.4 Prometheus alerting rules generated from the versioned rule set.

`configs/operations/alert-rules.json` is the only source of thresholds. This module turns
each rule into two Prometheus alerts on the scheduler's samples (`monitoring.cadence`):

- `<Name>`: the latest signal value breaches the threshold (reason `THRESHOLD`).
- `<Name>TelemetryMissing`: an expected signal has no value, or its value is older than
  `max_signal_age_seconds` (reason `TELEMETRY_MISSING`).

A fixed telemetry group adds alerts for a scrape target that is down or absent, a stale
scheduler, a stale alert evaluation, a failed collector or producer, and failed jobs or
tasks. So missing telemetry always alerts; it is never shown as healthy.

The deterministic stop does not depend on these alerts. The scheduler applies it first.
Prometheus routes notifications and feeds the dashboard. The rule file is committed at
`deploy/prometheus/rules/tennis-alerts.yml`; a test checks that it equals this output.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .alerts import AlertRule, AlertRuleSet, Control

TELEMETRY_RULES_VERSION = "f15-telemetry-rules-v1"
SCRAPE_JOBS = ("tennis-api", "tennis-scheduler", "prometheus", "alertmanager")
# Seconds. A tick is 60 s and the alert task runs every minute.
SCHEDULER_STALE_SECONDS = 300
EVALUATION_STALE_SECONDS = 300


def alert_name(rule_id: str) -> str:
    return "Tennis" + "".join(part.capitalize() for part in rule_id.split("-"))


def _selector(signal: str) -> str:
    return f'{{signal="{signal}"}}'


def threshold_expression(rule: AlertRule) -> str:
    return f"tennis_signal_value{_selector(rule.signal)} {rule.comparison.value} {rule.threshold}"


def missing_expression(rule: AlertRule, max_age_seconds: int) -> str:
    selector = _selector(rule.signal)
    return (
        f"(tennis_signal_expected{selector} unless on (signal, scope) "
        f"tennis_signal_value{selector}) or on (signal, scope) "
        f"(time() - tennis_signal_observed_timestamp_seconds{selector} > {max_age_seconds})"
    )


def _labels(rule: AlertRule, rules: AlertRuleSet, reason: str, control: Control) -> dict[str, str]:
    return {
        "severity": rule.severity.value.lower(),
        "rule_id": rule.rule_id,
        "rule_set": rules.version,
        "reason": reason,
        "control": control.value,
    }


def signal_group(rules: AlertRuleSet) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for rule in rules.rules:
        name = alert_name(rule.rule_id)
        entries.append(
            {
                "alert": name,
                "expr": threshold_expression(rule),
                "labels": _labels(rule, rules, "THRESHOLD", rule.control),
                "annotations": {
                    "summary": rule.description,
                    "scope": "{{ $labels.scope }}",
                },
            }
        )
        missing_control = rule.control if rule.missing_applies_control else Control.NONE
        entries.append(
            {
                "alert": f"{name}TelemetryMissing",
                "expr": missing_expression(rule, rules.max_signal_age_seconds),
                "labels": _labels(rule, rules, "TELEMETRY_MISSING", missing_control),
                "annotations": {
                    "summary": f"Signal {rule.signal} is missing or older than "
                    f"{rules.max_signal_age_seconds} s. Missing telemetry is not healthy.",
                    "scope": "{{ $labels.scope }}",
                },
            }
        )
    return {"name": "tennis-signal-rules", "rules": entries}


def _fixed(alert: str, expr: str, severity: str, summary: str, wait: str = "0m") -> dict[str, Any]:
    entry: dict[str, Any] = {"alert": alert, "expr": expr}
    if wait != "0m":
        entry["for"] = wait
    entry["labels"] = {
        "severity": severity,
        "rule_id": alert,
        "rule_set": TELEMETRY_RULES_VERSION,
        "reason": "TELEMETRY_MISSING",
        "control": Control.NONE.value,
    }
    entry["annotations"] = {"summary": summary}
    return entry


def telemetry_group() -> dict[str, Any]:
    jobs = "|".join(SCRAPE_JOBS)
    entries = [
        _fixed(
            "TennisScrapeTargetDown",
            f'up{{job=~"{jobs}"}} == 0',
            "critical",
            "A scrape target is down. Its metrics are missing, not healthy.",
            "2m",
        ),
    ]
    for job in SCRAPE_JOBS[:2]:
        suffix = "".join(part.capitalize() for part in job.split("-"))
        entries.append(
            _fixed(
                f"{suffix}TargetAbsent",
                f'absent(up{{job="{job}"}})',
                "critical",
                f"Prometheus has no scrape target for {job}.",
                "2m",
            )
        )
    entries += [
        _fixed(
            "TennisSchedulerStale",
            f"time() - tennis_scheduler_last_tick_timestamp_seconds > {SCHEDULER_STALE_SECONDS}"
            " or absent(tennis_scheduler_last_tick_timestamp_seconds)",
            "critical",
            "The scheduler has not completed a tick recently.",
            "1m",
        ),
        _fixed(
            "TennisAlertEvaluationStale",
            f"time() - tennis_alert_evaluation_timestamp_seconds > {EVALUATION_STALE_SECONDS}"
            " or absent(tennis_alert_evaluation_timestamp_seconds)",
            "critical",
            "The deterministic alert evaluation has not run recently.",
            "1m",
        ),
        _fixed(
            "TennisSignalExpectationsAbsent",
            "absent(tennis_signal_expected)",
            "critical",
            "No expected signal is exported. Every signal counts as missing.",
            "2m",
        ),
        _fixed(
            "TennisCollectorDown",
            "tennis_metrics_collector_up == 0",
            "critical",
            "A scrape-time collector failed. Its samples are missing.",
        ),
        _fixed(
            "TennisSignalProducerDown",
            "tennis_signal_producer_up == 0",
            "critical",
            "A signal producer failed. Its signals are missing.",
        ),
        _fixed(
            "TennisJobFailing",
            'increase(tennis_job_outcomes_total{state=~"FAILED|EXHAUSTED"}[15m]) > 0',
            "warning",
            "A scheduled job failed in the last 15 minutes.",
        ),
        _fixed(
            "TennisTaskFailing",
            'increase(tennis_task_runs_total{state="FAILED"}[15m]) > 0',
            "warning",
            "An operations task failed in the last 15 minutes.",
        ),
    ]
    return {"name": "tennis-telemetry", "rules": entries}


def rule_document(rules: AlertRuleSet) -> dict[str, Any]:
    return {"groups": [signal_group(rules), telemetry_group()]}


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    # A JSON string is a valid YAML double-quoted scalar.
    return json.dumps(str(value))


def to_yaml(value: object, indent: int = 0) -> list[str]:
    """Block-style YAML for mappings, sequences and scalars. Deterministic output."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, Mapping | list | tuple) and item:
                lines.append(f"{pad}{key}:")
                lines.extend(to_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(item) if item != [] else '[]'}")
        return lines
    if isinstance(value, Sequence) and not isinstance(value, str):
        for item in value:
            nested = to_yaml(item, indent + 1)
            if isinstance(item, Mapping) and nested:
                lines.append(f"{pad}- {nested[0].lstrip()}")
                lines.extend(nested[1:])
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return lines
    return [f"{pad}{_scalar(value)}"]


HEADER = (
    "# Generated by `tennis-ops prometheus-rules` from configs/operations/alert-rules.json.\n"
    "# Do not edit. Change the rule set with a new version, then regenerate.\n"
)


def render_rules(rules: AlertRuleSet) -> str:
    return HEADER + "\n".join(to_yaml(rule_document(rules))) + "\n"
