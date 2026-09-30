"""F15.3/F15.4 alert evaluation on a cadence, and its Prometheus samples.

The scheduler runs `AlertTask` once per window. It collects the signals, evaluates the
versioned rule set, applies the deterministic controls and keeps the result for the next
scrape. Prometheus evaluates the same thresholds on the exported samples (see
`monitoring.prometheus`) for routing and dashboards. The hard stop never waits for
Prometheus or an operator.

Exported families (one snapshot, replaced on each run, so an old value cannot linger):

- `tennis_signal_value{signal,scope}`: absent when the value is missing.
- `tennis_signal_observed_timestamp_seconds{signal,scope}`: when the value was measured.
- `tennis_signal_expected{signal,scope}`: 1 for each signal that a rule expects.
- `tennis_deterministic_alert{rule_id,severity,reason,scope}`: 1 for each active alert.
- `tennis_alert_evaluation_timestamp_seconds`: the time of the last evaluation.
"""

import json
import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from tennis_engine.common.clock import Clock, require_aware

from .alerts import Alert, AlertRuleSet, evaluate
from .controls import ControlAction
from .metrics import Family, MetricsRegistry, Sample
from .signals import GLOBAL, Signal

logger = logging.getLogger("tennis_engine.monitoring.cadence")

SIGNAL_VALUE = Family(
    "tennis_signal_value",
    "gauge",
    "Latest value of each monitoring signal. Absent when the value is missing.",
    ("signal", "scope"),
)
SIGNAL_OBSERVED = Family(
    "tennis_signal_observed_timestamp_seconds",
    "gauge",
    "Unix time at which each monitoring signal was measured.",
    ("signal", "scope"),
)
SIGNAL_EXPECTED = Family(
    "tennis_signal_expected",
    "gauge",
    "1 for each signal and scope that an alert rule expects.",
    ("signal", "scope"),
)
DETERMINISTIC_ALERT = Family(
    "tennis_deterministic_alert",
    "gauge",
    "1 for each alert from the scheduler's deterministic rule evaluation.",
    ("rule_id", "severity", "reason", "scope"),
)
EVALUATED = Family(
    "tennis_alert_evaluation_timestamp_seconds",
    "gauge",
    "Unix time of the last deterministic alert evaluation.",
    (),
)
# The collector declares every family it can return.
FAMILIES = (SIGNAL_VALUE, SIGNAL_OBSERVED, SIGNAL_EXPECTED, DETERMINISTIC_ALERT, EVALUATED)

SignalSource = Callable[[datetime], Iterable[Signal]]


def read_signal_inbox(directory: Path) -> tuple[tuple[Signal, ...], int]:
    """Signals that other jobs write as JSON lists. Returns the signals and the bad files."""
    if not directory.is_dir():
        return (), 0
    adapter = TypeAdapter(tuple[Signal, ...])
    found: list[Signal] = []
    invalid = 0
    for path in sorted(directory.glob("*.json")):
        try:
            found.extend(adapter.validate_json(path.read_bytes()))
        except (OSError, ValidationError, ValueError):
            invalid += 1
    return tuple(found), invalid


def expected_scopes(rules: AlertRuleSet, sources: Iterable[str]) -> tuple[tuple[str, str], ...]:
    scopes = sorted(set(sources))
    pairs: set[tuple[str, str]] = set()
    for rule in rules.rules:
        if rule.scope == "global":
            pairs.add((rule.signal, GLOBAL))
        else:
            pairs.update((rule.signal, source) for source in scopes)
    return tuple(sorted(pairs))


class AlertTask:
    def __init__(
        self,
        rules: AlertRuleSet,
        producers: Mapping[str, SignalSource],
        expected_sources: Callable[[datetime], Sequence[str]],
        apply: Callable[[Sequence[Alert]], Sequence[ControlAction]] | None,
        clock: Clock,
        registry: MetricsRegistry,
    ) -> None:
        self.rules = rules
        self.producers = dict(producers)
        self.expected_sources = expected_sources
        self.apply = apply
        self.clock = clock
        self._lock = threading.Lock()
        self._samples: list[Sample] = []
        self.producer_up = registry.gauge(
            "tennis_signal_producer_up",
            "1 when a signal producer ran in the last evaluation, 0 when it failed.",
            ("producer",),
        )
        self.controls = registry.counter(
            "tennis_controls_applied_total",
            "Deterministic stop controls applied by the scheduler, by control.",
            ("control",),
        )
        registry.register_collector("alert-task", self._collect, FAMILIES)

    def _collect(self) -> list[Sample]:
        with self._lock:
            return list(self._samples)

    def signals(self, now: datetime) -> tuple[Signal, ...]:
        collected: list[Signal] = []
        for name, producer in sorted(self.producers.items()):
            try:
                collected.extend(producer(now))
            except Exception as error:  # noqa: BLE001 - a failed producer is missing data
                self.producer_up.set(0, producer=name)
                logger.error(
                    "signal producer failed",
                    extra={"context": {"producer": name, "error": type(error).__name__}},
                )
                continue
            self.producer_up.set(1, producer=name)
        return tuple(collected)

    def __call__(self, window: datetime) -> Mapping[str, str]:
        now = require_aware(self.clock.now())
        signals = self.signals(now)
        try:
            sources = tuple(self.expected_sources(now))
        except Exception as error:  # noqa: BLE001 - unknown sources still evaluate the rest
            sources = ()
            logger.error(
                "expected sources unavailable", extra={"context": {"error": type(error).__name__}}
            )
        alerts = evaluate(self.rules, signals, sources=sources, now=now)
        actions: Sequence[ControlAction] = ()
        if alerts and self.apply is not None:
            actions = self.apply(alerts)
            for action in actions:
                if action.outcome == "APPLIED":
                    self.controls.inc(control=action.control.value)
        self._publish(signals, sources, alerts, now)
        if alerts:
            logger.warning(
                "alerts active",
                extra={
                    "context": {
                        "rule_set": self.rules.version,
                        "alerts": sorted({f"{a.rule_id}:{a.reason}" for a in alerts}),
                        "actions": len(actions),
                    }
                },
            )
        return {
            "rule_set": self.rules.version,
            "signals": str(len(signals)),
            "alerts": str(len(alerts)),
            "actions": str(len(actions)),
        }

    def _publish(
        self,
        signals: Sequence[Signal],
        sources: Sequence[str],
        alerts: Sequence[Alert],
        now: datetime,
    ) -> None:
        latest: dict[tuple[str, str], Signal] = {}
        for item in signals:
            key = (item.name, item.scope)
            if key not in latest or item.observed_at > latest[key].observed_at:
                latest[key] = item
        samples: list[Sample] = []
        for (name, scope), item in sorted(latest.items()):
            labels = (("signal", name), ("scope", scope))
            if item.value is not None:
                samples.append(Sample(SIGNAL_VALUE.name, float(item.value), labels))
            samples.append(Sample(SIGNAL_OBSERVED.name, item.observed_at.timestamp(), labels))
        for name, scope in expected_scopes(self.rules, sources):
            samples.append(Sample(SIGNAL_EXPECTED.name, 1, (("signal", name), ("scope", scope))))
        for alert in sorted({(a.rule_id, a.severity.value, a.reason, a.scope) for a in alerts}):
            names = ("rule_id", "severity", "reason", "scope")
            alert_labels = tuple(zip(names, alert, strict=True))
            samples.append(Sample(DETERMINISTIC_ALERT.name, 1, alert_labels))
        samples.append(Sample(EVALUATED.name, now.timestamp()))
        with self._lock:
            self._samples = samples


def write_signals(path: Path, signals: Sequence[Signal]) -> None:
    """Write a signal inbox file atomically, for jobs that measure a signal."""
    body = json.dumps([item.model_dump(mode="json") for item in signals], indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(body + "\n", encoding="utf-8")
    temporary.replace(path)
