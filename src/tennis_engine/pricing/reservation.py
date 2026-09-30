"""F12.6, F12.7 transactional exposure reservations for virtual decisions.

A reservation holds exposure for one decision key until it is committed to the virtual
ledger, released, or expires. Reservation and capacity checks run under one lock per
ledger, so concurrent workers cannot each consume the same budget, and a retried
decision key returns the original reservation. Committing records the virtual stake with
the reservation ID as the bet ID; while both exist the stake counts once, never zero.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from threading import Lock
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import Field

from tennis_engine.common.clock import Clock, require_aware
from tennis_engine.common.contracts import Contract, Identifier, Money, Timestamp
from tennis_engine.common.ids import stable_id
from tennis_engine.governance.service import reset_period
from tennis_engine.settlement.ledger import (
    LedgerEntry,
    LedgerView,
    VirtualBet,
    VirtualLedgerService,
)

from .risk import ExposureState, PeriodUsage


class ReservationEventKind(StrEnum):
    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"


class Reservation(Contract):
    reservation_id: UUID
    decision_key: Annotated[str, Field(min_length=1, max_length=300)]
    ledger_id: Identifier
    match_id: UUID
    bookmaker: Identifier
    selection_player_id: UUID
    stake: Money
    reserved_at: Timestamp
    expires_at: Timestamp


class ReservationEvent(Contract):
    reservation_id: UUID
    kind: ReservationEventKind
    recorded_at: Timestamp
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ReservationRejected(ValueError):
    """The stake does not fit the capacity that remains under the lock."""


class ReservationConflict(ValueError):
    pass


class ReservationUnit(Protocol):
    def reservations(self) -> Sequence[Reservation]: ...
    def events(self) -> Sequence[ReservationEvent]: ...
    def add_reservation(self, reservation: Reservation) -> None: ...
    def add_event(self, event: ReservationEvent) -> None: ...


class ReservationStore(Protocol):
    def unit(self, ledger_id: str) -> AbstractContextManager[ReservationUnit]: ...


class _MemoryUnit:
    def __init__(self, reservations: list[Reservation], events: list[ReservationEvent]) -> None:
        self._reservations = reservations
        self._events = events
        self._new_reservations: list[Reservation] = []
        self._new_events: list[ReservationEvent] = []

    def reservations(self) -> Sequence[Reservation]:
        return [*self._reservations, *self._new_reservations]

    def events(self) -> Sequence[ReservationEvent]:
        return [*self._events, *self._new_events]

    def add_reservation(self, reservation: Reservation) -> None:
        self._new_reservations.append(reservation)

    def add_event(self, event: ReservationEvent) -> None:
        self._new_events.append(event)

    def commit(self) -> None:
        self._reservations.extend(self._new_reservations)
        self._events.extend(self._new_events)


class MemoryReservationStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._reservations: dict[str, list[Reservation]] = {}
        self._events: dict[str, list[ReservationEvent]] = {}

    @contextmanager
    def unit(self, ledger_id: str) -> Iterator[ReservationUnit]:
        with self._lock:
            unit = _MemoryUnit(
                self._reservations.setdefault(ledger_id, []),
                self._events.setdefault(ledger_id, []),
            )
            yield unit
            unit.commit()


def active_reservations(
    unit: ReservationUnit, view: LedgerView, at: datetime
) -> tuple[Reservation, ...]:
    closed = {
        event.reservation_id
        for event in unit.events()
        if event.kind != ReservationEventKind.RESERVED and event.recorded_at <= at
    }
    booked = {bet.bet_id for bet in view.bets}
    return tuple(
        item
        for item in unit.reservations()
        if item.reserved_at <= at < item.expires_at
        and item.reservation_id not in closed
        and item.reservation_id not in booked
    )


def _usage(
    view: LedgerView, reservations: Sequence[Reservation], at: datetime, period: str
) -> PeriodUsage:
    start, end = reset_period(at, period)
    stakes = [bet.stake.amount for bet in view.bets if start <= bet.struck_at < end]
    stakes += [item.stake.amount for item in reservations if start <= item.reserved_at < end]
    return PeriodUsage(stake=sum(stakes, Decimal("0.00")), count=len(stakes))


def exposure_state(
    view: LedgerView,
    reservations: Sequence[Reservation],
    *,
    match_id: UUID,
    bookmaker: str,
    at: datetime,
) -> ExposureState:
    """Exposure from open virtual bets plus active reservations, in PLN."""
    open_bets = [bet for bet in view.bets if bet.bet_id in view.open_bet_ids]
    held = sum((item.stake.amount for item in reservations), Decimal("0.00"))
    positions = [(bet.match_id, bet.bookmaker, bet.stake.amount) for bet in open_bets] + [
        (item.match_id, item.bookmaker, item.stake.amount) for item in reservations
    ]
    return ExposureState(
        bankroll=max(Decimal("0.00"), view.balance - held),
        equity=view.equity,
        peak_bankroll=view.peak_equity,
        # Same-event positions add up, whatever the selection or bookmaker: opposite
        # sides of one match are not independent diversification.
        open_exposure=sum((stake for _, _, stake in positions), Decimal("0.00")),
        event_exposure=sum(
            (stake for match, _, stake in positions if match == match_id), Decimal("0.00")
        ),
        bookmaker_exposure=sum(
            (stake for _, book, stake in positions if book == bookmaker), Decimal("0.00")
        ),
        open_bets=len(positions),
        daily=_usage(view, reservations, at, "daily"),
        weekly=_usage(view, reservations, at, "weekly"),
        monthly=_usage(view, reservations, at, "monthly"),
    )


class ReservationRequest(Contract):
    decision_key: Annotated[str, Field(min_length=1, max_length=300)]
    ledger_id: Identifier
    match_id: UUID
    bookmaker: Identifier
    selection_player_id: UUID
    stake: Money
    ttl_seconds: Annotated[int, Field(gt=0, strict=True)]


class ExposureService:
    def __init__(self, store: ReservationStore, ledger: VirtualLedgerService, clock: Clock) -> None:
        self.store = store
        self.ledger = ledger
        self.clock = clock

    def exposure(
        self, ledger_id: str, *, match_id: UUID, bookmaker: str, at: datetime | None = None
    ) -> ExposureState:
        at = require_aware(at or self.clock.now())
        with self.store.unit(ledger_id) as unit:
            view = self.ledger.view(ledger_id)
            return exposure_state(
                view,
                active_reservations(unit, view, at),
                match_id=match_id,
                bookmaker=bookmaker,
                at=at,
            )

    def reserve(
        self,
        request: ReservationRequest,
        allowed_stake: Callable[[ExposureState], Decimal],
    ) -> Reservation:
        """Reserve under the ledger lock. `allowed_stake` recomputes capacity from the
        locked state, so a stale capacity from an earlier read cannot be used."""
        now = require_aware(self.clock.now())
        reservation_id = stable_id(
            "exposure-reservation", f"{request.ledger_id}:{request.decision_key}"
        )
        with self.store.unit(request.ledger_id) as unit:
            existing = next(
                (item for item in unit.reservations() if item.reservation_id == reservation_id),
                None,
            )
            if existing is not None:
                same = (
                    existing.match_id == request.match_id
                    and existing.bookmaker == request.bookmaker
                    and existing.selection_player_id == request.selection_player_id
                    and existing.stake == request.stake
                )
                if not same:
                    raise ReservationConflict("The decision key was reused with other terms")
                return existing
            view = self.ledger.view(request.ledger_id)
            state = exposure_state(
                view,
                active_reservations(unit, view, now),
                match_id=request.match_id,
                bookmaker=request.bookmaker,
                at=now,
            )
            if request.stake.amount <= 0 or request.stake.amount > allowed_stake(state):
                raise ReservationRejected("The stake exceeds the remaining capacity")
            reservation = Reservation(
                reservation_id=reservation_id,
                decision_key=request.decision_key,
                ledger_id=request.ledger_id,
                match_id=request.match_id,
                bookmaker=request.bookmaker,
                selection_player_id=request.selection_player_id,
                stake=request.stake,
                reserved_at=now,
                expires_at=now + timedelta(seconds=request.ttl_seconds),
            )
            unit.add_reservation(reservation)
            unit.add_event(
                ReservationEvent(
                    reservation_id=reservation_id,
                    kind=ReservationEventKind.RESERVED,
                    recorded_at=now,
                    reason=f"decision {request.decision_key}",
                )
            )
            return reservation

    def _close(
        self,
        unit: ReservationUnit,
        reservation: Reservation,
        kind: ReservationEventKind,
        reason: str,
    ) -> None:
        if not any(
            event.reservation_id == reservation.reservation_id and event.kind == kind
            for event in unit.events()
        ):
            unit.add_event(
                ReservationEvent(
                    reservation_id=reservation.reservation_id,
                    kind=kind,
                    recorded_at=require_aware(self.clock.now()),
                    reason=reason,
                )
            )

    def _find(self, unit: ReservationUnit, reservation_id: UUID) -> Reservation:
        found = next(
            (item for item in unit.reservations() if item.reservation_id == reservation_id), None
        )
        if found is None:
            raise ReservationConflict(f"Unknown reservation {reservation_id}")
        return found

    def commit(self, reservation_id: UUID, bet: VirtualBet) -> LedgerEntry:
        """Record the reserved stake as a virtual bet. Expired or released reservations fail."""
        now = require_aware(self.clock.now())
        with self.store.unit(bet.ledger_id) as unit:
            reservation = self._find(unit, reservation_id)
            if bet.bet_id != reservation_id or bet.stake != reservation.stake:
                raise ReservationConflict("The bet must match the reservation ID and stake")
            if bet.match_id != reservation.match_id or bet.bookmaker != reservation.bookmaker:
                raise ReservationConflict("The bet must match the reserved event and bookmaker")
            released = any(
                event.reservation_id == reservation_id
                and event.kind == ReservationEventKind.RELEASED
                for event in unit.events()
            )
            committed = any(
                event.reservation_id == reservation_id
                and event.kind == ReservationEventKind.COMMITTED
                for event in unit.events()
            )
            if released or (not committed and now >= reservation.expires_at):
                raise ReservationConflict("The reservation is no longer active")
            entry = self.ledger.record_virtual_bet(bet)
            self._close(unit, reservation, ReservationEventKind.COMMITTED, "virtual bet recorded")
            return entry

    def release(self, ledger_id: str, reservation_id: UUID, reason: str) -> None:
        with self.store.unit(ledger_id) as unit:
            reservation = self._find(unit, reservation_id)
            committed = any(
                event.reservation_id == reservation_id
                and event.kind == ReservationEventKind.COMMITTED
                for event in unit.events()
            )
            if committed:
                raise ReservationConflict("A committed reservation cannot be released")
            self._close(unit, reservation, ReservationEventKind.RELEASED, reason)
