"""Append-only decision storage for F14 reads.

A decision is written once. A retry with identical content is a no-op; other content
under the same ID is a conflict. A correction is a new superseding F12 record version.
"""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tennis_engine.contracts.domain import RecommendationStatus

from .contracts import StoredDecision


class DecisionConflict(ValueError):
    """Different content was offered for an existing decision ID."""


@dataclass(frozen=True)
class DecisionQuery:
    """Filters on the recorded decision. Ordering is scheduled start, then decision ID."""

    limit: int
    bookmaker: str | None = None
    statuses: frozenset[RecommendationStatus] = frozenset()
    starts_after: datetime | None = None
    starts_before: datetime | None = None
    match_id: UUID | None = None
    active_at: datetime | None = None
    latest_only: bool = False
    after: tuple[datetime, UUID] | None = None


class DecisionStore(Protocol):
    def add(self, stored: StoredDecision) -> None: ...

    def get(self, decision_id: UUID) -> StoredDecision | None: ...

    def query(self, query: DecisionQuery) -> Sequence[StoredDecision]: ...

    def successors(self, decision_id: UUID) -> Sequence[UUID]: ...

    def latest_observations(self) -> dict[str, datetime | None]: ...


def matches(stored: StoredDecision, query: DecisionQuery, superseded: set[UUID]) -> bool:
    record = stored.record
    start = stored.scheduled_start
    checks = (
        query.bookmaker is None or record.bookmaker == query.bookmaker,
        not query.statuses or record.status in query.statuses,
        query.starts_after is None or start >= query.starts_after,
        query.starts_before is None or start < query.starts_before,
        query.match_id is None or stored.context.match.match_id == query.match_id,
        query.active_at is None or record.expires_at > query.active_at,
        not query.latest_only or record.decision_id not in superseded,
        query.after is None or (start, record.decision_id) > query.after,
    )
    return all(checks)


class InMemoryDecisionStore:
    def __init__(self) -> None:
        self._items: dict[UUID, StoredDecision] = {}
        self._lock = threading.Lock()

    def add(self, stored: StoredDecision) -> None:
        with self._lock:
            existing = self._items.get(stored.record.decision_id)
            if existing is not None:
                if existing != stored:
                    raise DecisionConflict(str(stored.record.decision_id))
                return
            self._items[stored.record.decision_id] = stored

    def get(self, decision_id: UUID) -> StoredDecision | None:
        return self._items.get(decision_id)

    def query(self, query: DecisionQuery) -> Sequence[StoredDecision]:
        items = list(self._items.values())
        superseded = {item.record.supersedes for item in items if item.record.supersedes}
        selected = [item for item in items if matches(item, query, superseded)]
        selected.sort(key=lambda item: (item.scheduled_start, item.record.decision_id))
        return selected[: query.limit]

    def successors(self, decision_id: UUID) -> Sequence[UUID]:
        return sorted(
            item.record.decision_id
            for item in self._items.values()
            if item.record.supersedes == decision_id
        )

    def latest_observations(self) -> dict[str, datetime | None]:
        latest: dict[str, datetime | None] = {}
        for item in self._items.values():
            context = item.context
            for source_id in context.source_ids:
                latest.setdefault(source_id, None)
            if context.quote_source_id is not None and context.quote_observed_at is not None:
                current = latest.get(context.quote_source_id)
                if current is None or context.quote_observed_at > current:
                    latest[context.quote_source_id] = context.quote_observed_at
        return latest
