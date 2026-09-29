"""Feature-quality components and hard exclusions (F08.7).

A hard component must be ``PASS``. ``UNKNOWN`` on a hard component blocks, exactly like
``FAIL``. The soft score is informative only: a high average never compensates for an
unresolved identity, an unsupported format or missing odds or rules.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import model_validator

from tennis_engine.common.contracts import Contract, Identifier, Probability
from tennis_engine.normalization.contracts import BestOf

from .contracts import FeatureSnapshot


class QualityStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class Component(StrEnum):
    IDENTITY = "IDENTITY"
    TIMING = "TIMING"
    FORMAT = "FORMAT"
    STATS = "STATS"
    ODDS = "ODDS"
    RULES = "RULES"


HARD = frozenset(
    {Component.IDENTITY, Component.TIMING, Component.FORMAT, Component.ODDS, Component.RULES}
)


class QualityComponent(Contract):
    component: Component
    status: QualityStatus
    hard: bool
    score: Probability
    reason: Identifier | None = None


class QualityReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    components: tuple[QualityComponent, ...]
    usable: bool
    soft_score: Probability
    hard_failures: tuple[Identifier, ...]

    @model_validator(mode="after")
    def hard_rules(self) -> Self:
        blocking = [
            item for item in self.components if item.hard and item.status != QualityStatus.PASS
        ]
        if self.usable == bool(blocking):
            raise ValueError("usable must be false exactly when a hard component is not PASS")
        return self


def _component(
    component: Component, status: QualityStatus, score: Decimal, reason: str | None
) -> QualityComponent:
    return QualityComponent(
        component=component,
        status=status,
        hard=component in HARD,
        score=score,
        reason=reason if status != QualityStatus.PASS else None,
    )


def assess(
    snapshot: FeatureSnapshot,
    *,
    identity_resolved: bool | None,
    odds_ready: bool | None = None,
    rules_ready: bool | None = None,
    supported_best_of: frozenset[BestOf] = frozenset({BestOf.THREE}),
    minimum_stats_coverage: Decimal = Decimal("0.5"),
) -> QualityReport:
    """Assess one snapshot. ``None`` for identity, odds or rules means not yet known."""

    def tri(value: bool | None, reason: str) -> tuple[QualityStatus, Decimal, str]:
        if value is None:
            return QualityStatus.UNKNOWN, Decimal(0), f"{reason}_unknown"
        if value:
            return QualityStatus.PASS, Decimal(1), reason
        return QualityStatus.FAIL, Decimal(0), f"{reason}_failed"

    values = snapshot.values
    components = [_component(Component.IDENTITY, *tri(identity_resolved, "identity"))]

    has_schedule = any(item.kind == "schedule" for item in snapshot.inputs)
    components.append(
        _component(
            Component.TIMING,
            QualityStatus.PASS if has_schedule else QualityStatus.FAIL,
            Decimal(1) if has_schedule else Decimal(0),
            "schedule_unknown_at_cutoff",
        )
    )

    best_of = values.get("match.best_of")
    format_ok = best_of in {item.value for item in supported_best_of}
    components.append(
        _component(
            Component.FORMAT,
            QualityStatus.PASS if format_ok else QualityStatus.FAIL,
            Decimal(1) if format_ok else Decimal(0),
            "unsupported_or_unknown_format",
        )
    )

    coverage = [values.get(f"{side}.stats_coverage") for side in ("p1", "p2")]
    known = [item for item in coverage if isinstance(item, Decimal)]
    stats_score = min(known) if len(known) == 2 else Decimal(0)
    components.append(
        _component(
            Component.STATS,
            QualityStatus.PASS if stats_score >= minimum_stats_coverage else QualityStatus.FAIL,
            stats_score,
            "sparse_stats",
        )
    )
    components.append(_component(Component.ODDS, *tri(odds_ready, "odds")))
    components.append(_component(Component.RULES, *tri(rules_ready, "rules")))

    soft = [item.score for item in components if not item.hard]
    hard_failures = tuple(
        item.reason or item.component.value.lower()
        for item in components
        if item.hard and item.status != QualityStatus.PASS
    )
    return QualityReport(
        components=tuple(components),
        usable=not hard_failures,
        soft_score=sum(soft, Decimal(0)) / Decimal(len(soft)) if soft else Decimal(0),
        hard_failures=hard_failures,
    )
