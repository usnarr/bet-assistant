"""F15.3 monitoring signals: freshness, parser drift, missingness and distribution drift.

A signal is one measured value for one scope at one time. `value=None` means that the
measurement is missing. Alert evaluation treats a missing value as a breach, never as
healthy.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal

from tennis_engine.common.contracts import Contract, ExactDecimal, Identifier, Timestamp
from tennis_engine.ingestion.bookmakers.adapter import SnapshotMetrics
from tennis_engine.serving.contracts import SourceHealth
from tennis_engine.settlement.ledger import LedgerReconciliation

GLOBAL = "global"
PROBABILITY_EDGES = tuple(Decimal(index) / 10 for index in range(1, 10))


class Signal(Contract):
    name: Identifier
    scope: Identifier = GLOBAL
    value: ExactDecimal | None
    observed_at: Timestamp


def freshness_signals(health: Iterable[SourceHealth], now: datetime) -> tuple[Signal, ...]:
    """Quote observation age per source. A source without an observation has no value."""
    return tuple(
        Signal(
            name="source_observation_age_seconds",
            scope=row.source_id,
            value=None if row.age_seconds is None else Decimal(row.age_seconds),
            observed_at=now,
        )
        for row in health
    )


def parser_signals(
    source_id: str,
    current: SnapshotMetrics,
    baseline: SnapshotMetrics | None,
    now: datetime,
) -> tuple[Signal, ...]:
    """The blueprint section 10.9 drift measurements for one parsed snapshot."""
    volume_change: Decimal | None = None
    if baseline is not None and baseline.events > 0:
        volume_change = abs(Decimal(current.events - baseline.events)) / baseline.events
    values: dict[str, Decimal | None] = {
        "parser_events": Decimal(current.events),
        "parser_valid_rate": current.valid_rate,
        "parser_unknown_label_rate": current.unresolved_label_rate,
        "parser_duplicate_selection_ids": Decimal(current.duplicate_selection_ids),
        "parser_volume_change": volume_change,
    }
    return tuple(
        Signal(name=name, scope=source_id, value=value, observed_at=now)
        for name, value in values.items()
        # Without a baseline there is no volume change to measure; that is not missing data.
        if not (name == "parser_volume_change" and baseline is None)
    )


def missing_rate(present: Sequence[bool]) -> Decimal | None:
    """Share of missing values. None when there are no rows to measure."""
    if not present:
        return None
    return Decimal(sum(not item for item in present)) / len(present)


def population_stability_index(
    expected: Sequence[Decimal],
    actual: Sequence[Decimal],
    edges: Sequence[Decimal] = PROBABILITY_EDGES,
    floor: Decimal = Decimal("0.0001"),
) -> Decimal | None:
    """PSI of `actual` against `expected` over fixed bins. None when a sample is empty.

    The default edges suit probabilities in [0, 1]. Empty bins use `floor` so the index
    stays finite. A common reading is: below 0.1 stable, above 0.25 a large shift. These
    readings are conventions, not validated thresholds for this system.
    """
    if not expected or not actual:
        return None
    bounds = sorted(edges)

    def shares(values: Sequence[Decimal]) -> list[Decimal]:
        counts = [0] * (len(bounds) + 1)
        for value in values:
            index = sum(value >= bound for bound in bounds)
            counts[index] += 1
        return [max(Decimal(count) / len(values), floor) for count in counts]

    total = Decimal(0)
    for base, now in zip(shares(expected), shares(actual), strict=True):
        total += (now - base) * (now / base).ln()
    return total.quantize(Decimal("0.000001"))


def ledger_signals(reconciliations: Iterable[LedgerReconciliation], now: datetime) -> Signal:
    """Count of virtual ledgers whose entry chain does not balance."""
    mismatches = sum(not item.balanced for item in reconciliations)
    return Signal(name="settlement_mismatch_count", value=Decimal(mismatches), observed_at=now)
