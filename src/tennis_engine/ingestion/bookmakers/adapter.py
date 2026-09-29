"""F05 adapter boundary, F03 parser bridge and F05.8 parser drift metrics.

Collection itself goes through the F03 approved fetchers, which recheck the F01 source
policy at execution time. An adapter adds the bookmaker parser, its rule references and a
health check that fails while the source is not approved.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from tennis_engine.common.contracts import Contract, Identifier
from tennis_engine.governance.contracts import Purpose
from tennis_engine.governance.service import GovernanceService, PermissionDenied
from tennis_engine.ingestion.contracts import ParsedItem

from .contracts import BookmakerParser, Listing, Snapshot


def snapshot_items(snapshot: Listing) -> tuple[ParsedItem, ...]:
    """Derived F03 records. Observation time stays on the F03 observation."""
    events = tuple(
        ParsedItem(
            record_type="bookmaker-event",
            natural_key=f"{snapshot.bookmaker}:{event.source_event_id}",
            payload=event.model_dump(mode="json"),
        )
        for event in snapshot.events
    )
    quotes = tuple(
        ParsedItem(
            record_type="bookmaker-quote",
            natural_key=":".join(quote.key),
            payload=quote.model_dump(mode="json"),
        )
        for quote in snapshot.quotes
    )
    rejected = tuple(
        ParsedItem(
            record_type="bookmaker-rejected-record",
            natural_key=f"{snapshot.bookmaker}:{index}",
            payload=item.model_dump(mode="json"),
        )
        for index, item in enumerate(snapshot.rejected)
    )
    return events + quotes + rejected


class SnapshotMetrics(Contract):
    bookmaker: Identifier
    parser_version: Identifier
    events: int
    markets: int
    selections: int
    rejected_records: int
    supported_selections: int
    unknown_market_labels: int
    null_start_events: int
    duplicate_selection_ids: int

    @property
    def valid_rate(self) -> Decimal:
        total = self.selections + self.rejected_records
        return Decimal(1) if total == 0 else Decimal(self.selections) / total

    @property
    def unresolved_label_rate(self) -> Decimal:
        return (
            Decimal(0)
            if self.selections == 0
            else Decimal(self.unknown_market_labels) / self.selections
        )


def snapshot_metrics(snapshot: Snapshot) -> SnapshotMetrics:
    selection_ids = [quote.source_selection_id for quote in snapshot.quotes]
    return SnapshotMetrics(
        bookmaker=snapshot.bookmaker,
        parser_version=snapshot.parser_version,
        events=len(snapshot.events),
        markets=len({(quote.source_event_id, quote.source_market_id) for quote in snapshot.quotes}),
        selections=len(snapshot.quotes),
        rejected_records=len(snapshot.rejected),
        supported_selections=sum(quote.market is not None for quote in snapshot.quotes),
        unknown_market_labels=sum(quote.market is None for quote in snapshot.quotes),
        null_start_events=sum(event.scheduled_start is None for event in snapshot.events),
        duplicate_selection_ids=len(selection_ids) - len(set(selection_ids)),
    )


class DriftAlert(StrEnum):
    ZERO_EVENTS = "ZERO_EVENTS"
    VOLUME_CHANGE = "VOLUME_CHANGE"
    INVALID_RECORDS = "INVALID_RECORDS"
    UNKNOWN_LABELS = "UNKNOWN_LABELS"
    DUPLICATE_SELECTION_IDS = "DUPLICATE_SELECTION_IDS"


class DriftThresholds(Contract):
    """Proposed initial thresholds (blueprint section 10.9); tune per bookmaker."""

    version: Identifier = "parser-drift-proposed-v1"
    max_volume_change: Annotated[Decimal, Field(gt=0)] = Decimal("0.50")
    min_valid_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.99")
    max_unknown_label_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.02")


def drift_alerts(
    current: SnapshotMetrics,
    baseline: SnapshotMetrics | None,
    thresholds: DriftThresholds,
    *,
    expect_events: bool = True,
) -> tuple[DriftAlert, ...]:
    alerts: list[DriftAlert] = []
    if expect_events and current.events == 0:
        alerts.append(DriftAlert.ZERO_EVENTS)
    if baseline is not None and baseline.events > 0:
        change = abs(Decimal(current.events - baseline.events)) / baseline.events
        if change > thresholds.max_volume_change:
            alerts.append(DriftAlert.VOLUME_CHANGE)
    if current.valid_rate < thresholds.min_valid_rate:
        alerts.append(DriftAlert.INVALID_RECORDS)
    if current.unresolved_label_rate > thresholds.max_unknown_label_rate:
        alerts.append(DriftAlert.UNKNOWN_LABELS)
    if current.duplicate_selection_ids:
        alerts.append(DriftAlert.DUPLICATE_SELECTION_IDS)
    return tuple(alerts)


@dataclass(frozen=True)
class BookmakerAdapter:
    """One bookmaker integration. It never fetches by itself and never places bets."""

    bookmaker: str
    source_id: str
    parser: BookmakerParser
    payout_rule_version: str
    settlement_rule_version: str

    @property
    def parser_version(self) -> str:
        return self.parser.version

    def healthcheck(self, governance: GovernanceService, purpose: Purpose) -> None:
        """Raise while the F01 source policy does not allow collection."""
        decision = governance.can_fetch(self.source_id, purpose)
        if not decision.allowed:
            raise PermissionDenied(decision)

    def parse(self, body: bytes) -> tuple[ParsedItem, ...]:
        """F03 `SourceParser` entry point for archive-before-parse and replay."""
        return snapshot_items(self.parser.parse_listing(body))

    @property
    def version(self) -> str:
        return self.parser.version

    def discover_events(self, snapshot: Snapshot) -> tuple[str, ...]:
        return tuple(event.source_event_id for event in snapshot.events)
