"""F15.2 resource leases with expiry and fencing tokens (blueprint section 33.3).

A lease names one resource, for example `source:<id>:<resource>` or `job:<name>:<scope>`.
Each new acquisition gets a larger fencing token. A renewal keeps the token. An effect is
valid only when its token is still the current token and the lease has not expired. A
stale worker therefore cannot publish after another worker takes the lease.
"""

import threading
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from tennis_engine.common.clock import require_aware
from tennis_engine.common.contracts import Contract, Timestamp


class LeaseLost(RuntimeError):
    """The lease expired or another holder has a newer fencing token."""


class Lease(Contract):
    resource: str
    owner: str
    fencing_token: int
    acquired_at: Timestamp
    expires_at: Timestamp

    def valid_at(self, now: datetime) -> bool:
        return require_aware(now) < self.expires_at


class LeaseStore(Protocol):
    def acquire(self, resource: str, owner: str, ttl: timedelta, now: datetime) -> Lease | None: ...

    def renew(self, lease: Lease, ttl: timedelta, now: datetime) -> Lease: ...

    def release(self, lease: Lease) -> None: ...

    def current(self, resource: str) -> Lease | None: ...


def require_current(lease: Lease, current: Lease | None, now: datetime) -> None:
    if current is None or current.fencing_token != lease.fencing_token:
        raise LeaseLost(f"A newer lease holds {lease.resource}")
    if current.owner != lease.owner or not current.valid_at(now):
        raise LeaseLost(f"The lease on {lease.resource} has expired")


def _check_ttl(ttl: timedelta) -> None:
    if ttl <= timedelta(0) or ttl > timedelta(hours=1):
        raise ValueError("A lease lasts more than 0 seconds and at most one hour")


class InMemoryLeaseStore:
    def __init__(self) -> None:
        self._leases: dict[str, Lease] = {}
        self._lock = threading.Lock()

    def acquire(self, resource: str, owner: str, ttl: timedelta, now: datetime) -> Lease | None:
        _check_ttl(ttl)
        now = require_aware(now)
        with self._lock:
            held = self._leases.get(resource)
            if held is not None and held.valid_at(now) and held.owner != owner:
                return None
            if held is not None and held.valid_at(now):
                return held
            token = held.fencing_token + 1 if held is not None else 1
            lease = Lease(
                resource=resource,
                owner=owner,
                fencing_token=token,
                acquired_at=now,
                expires_at=now + ttl,
            )
            self._leases[resource] = lease
            return lease

    def renew(self, lease: Lease, ttl: timedelta, now: datetime) -> Lease:
        _check_ttl(ttl)
        with self._lock:
            require_current(lease, self._leases.get(lease.resource), now)
            renewed = lease.model_copy(update={"expires_at": require_aware(now) + ttl})
            self._leases[lease.resource] = renewed
            return renewed

    def release(self, lease: Lease) -> None:
        with self._lock:
            held = self._leases.get(lease.resource)
            if held is not None and held.fencing_token == lease.fencing_token:
                # Keep the token so the next holder still gets a larger one.
                self._leases[lease.resource] = held.model_copy(
                    update={"expires_at": held.acquired_at + timedelta(microseconds=1)}
                )

    def current(self, resource: str) -> Lease | None:
        return self._leases.get(resource)


def _lease(row: Any) -> Lease:
    return Lease.model_validate(dict(row._mapping))


class PostgresLeaseStore:
    """Leases in `tennis.resource_lease`. The token check and the effect share a transaction."""

    SELECT = (
        "SELECT resource, owner, fencing_token, acquired_at, expires_at "
        "FROM tennis.resource_lease WHERE resource = :resource"
    )

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def acquire(self, resource: str, owner: str, ttl: timedelta, now: datetime) -> Lease | None:
        _check_ttl(ttl)
        now = require_aware(now)
        with self.engine.begin() as db:
            row = db.execute(
                text(
                    "INSERT INTO tennis.resource_lease AS l (resource, owner, fencing_token, "
                    "acquired_at, expires_at) VALUES (:resource, :owner, 1, :now, :expires) "
                    "ON CONFLICT (resource) DO UPDATE SET owner = EXCLUDED.owner, "
                    "fencing_token = l.fencing_token + 1, acquired_at = EXCLUDED.acquired_at, "
                    "expires_at = EXCLUDED.expires_at WHERE l.expires_at <= :now "
                    "RETURNING resource, owner, fencing_token, acquired_at, expires_at"
                ),
                {"resource": resource, "owner": owner, "now": now, "expires": now + ttl},
            ).first()
            if row is not None:
                return _lease(row)
            held = db.execute(text(self.SELECT), {"resource": resource}).first()
        if held is not None and held.owner == owner:
            return _lease(held)
        return None

    def renew(self, lease: Lease, ttl: timedelta, now: datetime) -> Lease:
        _check_ttl(ttl)
        now = require_aware(now)
        with self.engine.begin() as db:
            self.lock_current(db, lease, now)
            row = db.execute(
                text(
                    "UPDATE tennis.resource_lease SET expires_at = :expires "
                    "WHERE resource = :resource "
                    "RETURNING resource, owner, fencing_token, acquired_at, expires_at"
                ),
                {"resource": lease.resource, "expires": now + ttl},
            ).one()
        return _lease(row)

    def release(self, lease: Lease) -> None:
        with self.engine.begin() as db:
            db.execute(
                text(
                    "UPDATE tennis.resource_lease SET expires_at = acquired_at + "
                    "interval '1 microsecond' WHERE resource = :resource "
                    "AND fencing_token = :token"
                ),
                {"resource": lease.resource, "token": lease.fencing_token},
            )

    def current(self, resource: str) -> Lease | None:
        with self.engine.connect() as db:
            row = db.execute(text(self.SELECT), {"resource": resource}).first()
        return _lease(row) if row is not None else None

    def lock_current(self, db: Connection, lease: Lease, now: datetime) -> None:
        """Lock the lease row and raise `LeaseLost` unless `lease` is still current."""
        row = db.execute(text(self.SELECT + " FOR UPDATE"), {"resource": lease.resource}).first()
        require_current(lease, _lease(row) if row is not None else None, now)
