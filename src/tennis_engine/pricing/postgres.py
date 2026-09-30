"""PostgreSQL persistence for F12 exposure reservations.

A unit holds a transaction-scoped advisory lock per ledger. Reservation decisions and
commits run inside it, so two workers cannot reserve the same remaining budget.
"""

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from .reservation import Reservation, ReservationEvent, ReservationUnit


class _PostgresUnit:
    def __init__(self, connection: Connection, ledger_id: str) -> None:
        self._db = connection
        self._ledger_id = ledger_id

    def reservations(self) -> Sequence[Reservation]:
        rows = self._db.execute(
            text(
                "SELECT payload FROM tennis.risk_reservation WHERE ledger_id = :ledger "
                "ORDER BY reserved_at, reservation_id"
            ),
            {"ledger": self._ledger_id},
        )
        return [Reservation.model_validate(row[0]) for row in rows]

    def events(self) -> Sequence[ReservationEvent]:
        rows = self._db.execute(
            text(
                "SELECT e.reservation_id, e.kind, e.recorded_at, e.reason "
                "FROM tennis.risk_reservation_event e JOIN tennis.risk_reservation r "
                "USING (reservation_id) WHERE r.ledger_id = :ledger ORDER BY e.event_id"
            ),
            {"ledger": self._ledger_id},
        )
        return [
            ReservationEvent(
                reservation_id=row.reservation_id,
                kind=row.kind,
                recorded_at=row.recorded_at,
                reason=row.reason,
            )
            for row in rows
        ]

    def add_reservation(self, reservation: Reservation) -> None:
        self._db.execute(
            text(
                "INSERT INTO tennis.risk_reservation (reservation_id, decision_key, ledger_id, "
                "match_id, bookmaker, stake, reserved_at, expires_at, payload) VALUES (:id, "
                ":key, :ledger, :match_id, :bookmaker, :stake, :reserved_at, :expires_at, "
                "CAST(:payload AS JSONB))"
            ),
            {
                "id": reservation.reservation_id,
                "key": reservation.decision_key,
                "ledger": reservation.ledger_id,
                "match_id": reservation.match_id,
                "bookmaker": reservation.bookmaker,
                "stake": reservation.stake.amount,
                "reserved_at": reservation.reserved_at,
                "expires_at": reservation.expires_at,
                "payload": json.dumps(reservation.model_dump(mode="json"), sort_keys=True),
            },
        )

    def add_event(self, event: ReservationEvent) -> None:
        self._db.execute(
            text(
                "INSERT INTO tennis.risk_reservation_event (reservation_id, kind, recorded_at, "
                "reason) VALUES (:id, :kind, :recorded_at, :reason)"
            ),
            {
                "id": event.reservation_id,
                "kind": event.kind.value,
                "recorded_at": event.recorded_at,
                "reason": event.reason,
            },
        )


class PostgresReservationStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def unit(self, ledger_id: str) -> Iterator[ReservationUnit]:
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('risk-reservation:' || :ledger))"),
                {"ledger": ledger_id},
            )
            yield _PostgresUnit(connection, ledger_id)
