"""F15.4 versioned alert rules and deterministic evaluation.

A rule compares one signal with a threshold. A missing or stale signal breaches the rule
(`TELEMETRY_MISSING`). Only a CRITICAL rule can name a control. Controls only stop; no rule
can resume a source or turn a stop off.
"""

import json
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from tennis_engine.common.clock import require_aware
from tennis_engine.common.contracts import Contract, ExactDecimal, Identifier, Timestamp

from .signals import GLOBAL, Signal


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


class Control(StrEnum):
    NONE = "NONE"
    SOURCE_STOP = "SOURCE_STOP"
    GLOBAL_STOP = "GLOBAL_STOP"


class Comparison(StrEnum):
    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="

    def breached(self, value: Decimal, threshold: Decimal) -> bool:
        return {
            Comparison.GT: value > threshold,
            Comparison.GE: value >= threshold,
            Comparison.LT: value < threshold,
            Comparison.LE: value <= threshold,
        }[self]


class AlertRule(Contract):
    rule_id: Identifier
    signal: Identifier
    scope: Literal["source", "global"]
    comparison: Comparison
    threshold: ExactDecimal
    severity: Severity
    control: Control = Control.NONE
    # Missing telemetry always raises the alert. This says if it also applies the control.
    missing_applies_control: bool = True
    description: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def control_matches_severity(self) -> Self:
        if self.control != Control.NONE and self.severity != Severity.CRITICAL:
            raise ValueError("Only a CRITICAL rule can apply a control")
        if self.control == Control.SOURCE_STOP and self.scope != "source":
            raise ValueError("SOURCE_STOP needs a source-scoped rule")
        return self


class AlertRuleSet(Contract):
    version: Identifier
    # ACCEPTED: the owner accepted the thresholds for a stated use. It needs a date,
    # the accepting party and a note that says when the thresholds are reviewed again.
    status: Literal["PROPOSED", "ACCEPTED", "APPROVED"]
    accepted_on: date | None = None
    accepted_by: str | None = Field(default=None, min_length=1, max_length=64)
    accepted_for: str | None = Field(default=None, min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)
    # A signal older than this is missing telemetry.
    max_signal_age_seconds: Annotated[int, Field(gt=0, le=86_400, strict=True)]
    rules: tuple[AlertRule, ...]

    @model_validator(mode="after")
    def unique_rules(self) -> Self:
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Rule IDs must be unique")
        if self.status == "ACCEPTED" and not (
            self.accepted_on and self.accepted_by and self.accepted_for and self.note
        ):
            raise ValueError("An accepted rule set needs a date, a party, a use and a note")
        return self


class Alert(Contract):
    rule_id: Identifier
    rule_set_version: Identifier
    severity: Severity
    control: Control
    signal: Identifier
    scope: Identifier
    reason: Literal["THRESHOLD", "TELEMETRY_MISSING"]
    value: ExactDecimal | None
    threshold: ExactDecimal
    evaluated_at: Timestamp


def load_rules(path: Path) -> AlertRuleSet:
    return AlertRuleSet.model_validate(json.loads(path.read_text(encoding="utf-8")))


def evaluate(
    rules: AlertRuleSet,
    signals: Iterable[Signal],
    *,
    sources: Iterable[str],
    now: datetime,
) -> tuple[Alert, ...]:
    """Alerts for every breached rule. `sources` are the scopes each source rule expects."""
    now = require_aware(now)
    oldest = now - timedelta(seconds=rules.max_signal_age_seconds)
    latest: dict[tuple[str, str], Signal] = {}
    for item in signals:
        key = (item.name, item.scope)
        if key not in latest or item.observed_at > latest[key].observed_at:
            latest[key] = item
    expected_sources = sorted(set(sources))
    alerts: list[Alert] = []
    for rule in rules.rules:
        scopes = expected_sources if rule.scope == "source" else [GLOBAL]
        if rule.scope == "source":
            # A source that reports a signal but is not expected is still evaluated.
            scopes = sorted(
                set(scopes) | {scope for name, scope in latest if name == rule.signal} - {GLOBAL}
            )
        for scope in scopes:
            signal = latest.get((rule.signal, scope))
            value = signal.value if signal is not None else None
            fresh = signal is not None and oldest <= signal.observed_at <= now
            if value is None or not fresh:
                reason: Literal["THRESHOLD", "TELEMETRY_MISSING"] = "TELEMETRY_MISSING"
            elif rule.comparison.breached(value, rule.threshold):
                reason = "THRESHOLD"
            else:
                continue
            alerts.append(
                Alert(
                    rule_id=rule.rule_id,
                    rule_set_version=rules.version,
                    severity=rule.severity,
                    control=(
                        rule.control
                        if reason == "THRESHOLD" or rule.missing_applies_control
                        else Control.NONE
                    ),
                    signal=rule.signal,
                    scope=scope,
                    reason=reason,
                    value=value if fresh else None,
                    threshold=rule.threshold,
                    evaluated_at=now,
                )
            )
    return tuple(alerts)
