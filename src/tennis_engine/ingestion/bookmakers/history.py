"""F05.4 append-only quote history: polls, quote observations and event mappings.

Writes are idempotent. Nothing is updated in place: a price change, suspension, source
correction or cancellation is a new observation, and intervals are derived on read.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from threading import Lock
from typing import Protocol
from uuid import UUID

from tennis_engine.common.clock import require_aware
from tennis_engine.common.contracts import Contract, Identifier, Timestamp
from tennis_engine.common.ids import stable_id
from tennis_engine.normalization.contracts import MatchResolution

from .contracts import Snapshot
from .mapping import MappedSnapshot
from .quotes import (
    Actionability,
    ActionabilityPolicy,
    QuoteInterval,
    QuoteObservation,
    build_intervals,
    evaluate_actionability,
)

QuoteKey = tuple[str, str, str, str]


class PollRecord(Contract):
    poll_id: UUID
    bookmaker: Identifier
    parser_version: Identifier
    observed_at: Timestamp
    raw_content_sha256: str
    source_event_ids: tuple[str, ...]


class HistoryStore(Protocol):
    def add_poll(self, poll: PollRecord) -> bool: ...
    def add_observation(self, observation_id: UUID, observation: QuoteObservation) -> bool: ...
    def add_mapping(
        self, mapping_id: UUID, bookmaker: str, resolution: MatchResolution
    ) -> bool: ...
    def observations(self, key: QuoteKey, until: datetime) -> Sequence[QuoteObservation]: ...
    def event_polls(
        self, bookmaker: str, source_event_id: str, until: datetime
    ) -> Sequence[datetime]: ...
    def mappings(
        self, bookmaker: str, source_event_id: str, until: datetime
    ) -> Sequence[MatchResolution]: ...


class MemoryHistoryStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._polls: dict[UUID, PollRecord] = {}
        self._observations: dict[UUID, QuoteObservation] = {}
        self._mappings: dict[UUID, tuple[str, MatchResolution]] = {}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            yield

    def add_poll(self, poll: PollRecord) -> bool:
        with self._locked():
            if poll.poll_id in self._polls:
                return False
            self._polls[poll.poll_id] = poll
            return True

    def add_observation(self, observation_id: UUID, observation: QuoteObservation) -> bool:
        with self._locked():
            if observation_id in self._observations:
                if self._observations[observation_id] != observation:
                    raise ValueError("An observation ID was reused with other content")
                return False
            self._observations[observation_id] = observation
            return True

    def add_mapping(self, mapping_id: UUID, bookmaker: str, resolution: MatchResolution) -> bool:
        with self._locked():
            if mapping_id in self._mappings:
                return False
            self._mappings[mapping_id] = (bookmaker, resolution)
            return True

    def observations(self, key: QuoteKey, until: datetime) -> Sequence[QuoteObservation]:
        return sorted(
            (
                item
                for item in self._observations.values()
                if item.quote.key == key and item.observed_at <= until
            ),
            key=lambda item: (item.observed_at, item.raw_content_sha256),
        )

    def event_polls(
        self, bookmaker: str, source_event_id: str, until: datetime
    ) -> Sequence[datetime]:
        return sorted(
            poll.observed_at
            for poll in self._polls.values()
            if poll.bookmaker == bookmaker
            and source_event_id in poll.source_event_ids
            and poll.observed_at <= until
        )

    def mappings(
        self, bookmaker: str, source_event_id: str, until: datetime
    ) -> Sequence[MatchResolution]:
        return sorted(
            (
                resolution
                for book, resolution in self._mappings.values()
                if book == bookmaker
                and resolution.source_event_id == source_event_id
                and resolution.resolved_at <= until
            ),
            key=lambda item: item.resolved_at,
        )


class QuoteHistory:
    def __init__(self, store: HistoryStore) -> None:
        self.store = store

    def record(self, snapshot: Snapshot, mapped: MappedSnapshot | None = None) -> int:
        """Record one successful poll. Returns the number of new quote observations."""
        poll_key = f"{snapshot.bookmaker}:{snapshot.observed_at.isoformat()}"
        self.store.add_poll(
            PollRecord(
                poll_id=stable_id("bookmaker-poll", f"{poll_key}:{snapshot.raw_content_sha256}"),
                bookmaker=snapshot.bookmaker,
                parser_version=snapshot.parser_version,
                observed_at=snapshot.observed_at,
                raw_content_sha256=snapshot.raw_content_sha256,
                source_event_ids=tuple(event.source_event_id for event in snapshot.events),
            )
        )
        events = {event.source_event_id: event for event in snapshot.events}
        added = 0
        for quote in snapshot.quotes:
            event = events[quote.source_event_id]
            observation = QuoteObservation(
                quote=quote,
                observed_at=snapshot.observed_at,
                parser_version=snapshot.parser_version,
                raw_content_sha256=snapshot.raw_content_sha256,
                scheduled_start=event.scheduled_start,
                event_state=event.state,
            )
            identity = ":".join(
                (*quote.key, snapshot.observed_at.isoformat(), snapshot.raw_content_sha256)
            )
            added += self.store.add_observation(
                stable_id("bookmaker-quote-observation", identity), observation
            )
        for resolution in mapped.resolutions if mapped else ():
            self.store.add_mapping(
                stable_id(
                    "bookmaker-event-mapping",
                    f"{snapshot.bookmaker}:{resolution.source_event_id}:"
                    f"{resolution.resolved_at.isoformat()}:{snapshot.raw_content_sha256}",
                ),
                snapshot.bookmaker,
                resolution,
            )
        return added

    def intervals(self, key: QuoteKey, *, until: datetime) -> tuple[QuoteInterval, ...]:
        return build_intervals(self.store.observations(key, require_aware(until)))

    def actionability(
        self, key: QuoteKey, *, at: datetime, policy: ActionabilityPolicy
    ) -> Actionability:
        at = require_aware(at)
        return evaluate_actionability(
            self.store.observations(key, at),
            at=at,
            policy=policy,
            event_polls=self.store.event_polls(key[0], key[1], at),
        )
