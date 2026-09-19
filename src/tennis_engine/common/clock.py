"""UTC clock abstractions keep time-dependent behavior deterministic in tests."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware datetime required")
    return value.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class FrozenClock:
    instant: datetime

    def __post_init__(self) -> None:
        self.instant = require_aware(self.instant)

    def now(self) -> datetime:
        return self.instant

    def advance(self, delta: timedelta) -> datetime:
        if delta.total_seconds() < 0:
            raise ValueError("A clock cannot be moved backwards")
        self.instant += delta
        return self.instant
