"""F14.8 read-time checks. They run on every current read, never from a cache.

A check that cannot run fails closed: the record is shown as not actionable with a reason.
"""

import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Literal, Protocol

from tennis_engine.common.clock import require_aware
from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.governance.contracts import Purpose, ResponsibleUsePolicy
from tennis_engine.governance.service import GovernanceService, PolicyLookup
from tennis_engine.ingestion.bookmakers.history import QuoteHistory
from tennis_engine.ingestion.bookmakers.quotes import Actionability, ActionabilityPolicy
from tennis_engine.pricing.decision import volatile_problems

from .contracts import ResponsibleUseStatus, SourceHealth, StoredDecision

ActionabilityLookup = Callable[[StoredDecision, datetime], Actionability | None]


class ReadChecks(Protocol):
    def check(self, stored: StoredDecision, now: datetime) -> tuple[str, ...]:
        """Reasons why this record is not actionable now. Empty means every check passed."""
        ...

    def responsible_use(self, account_scope: str, now: datetime) -> ResponsibleUseStatus: ...

    def source_state(self, source_id: str, now: datetime) -> str | None:
        """None when the source is enabled, else the denial reason."""
        ...


class RedistributionPolicy(Protocol):
    def allows(self, source_id: str, now: datetime) -> bool: ...


class NoRedistribution:
    """Default: no source permits redistribution until governance approves it."""

    def allows(self, source_id: str, now: datetime) -> bool:
        return False


class StaticRedistribution:
    """Explicit allow list, for synthetic fixtures and local development only."""

    def __init__(self, source_ids: Iterable[str]) -> None:
        self._allowed = frozenset(source_ids)

    def allows(self, source_id: str, now: datetime) -> bool:
        return source_id in self._allowed


GovernanceFactory = Callable[[], GovernanceService]


def per_thread(factory: GovernanceFactory) -> GovernanceFactory:
    """One governance service per thread. A SQLite connection cannot cross threads."""
    local = threading.local()

    def get() -> GovernanceService:
        service: GovernanceService | None = getattr(local, "service", None)
        if service is None:
            service = factory()
            local.service = service
        return service

    return get


class GovernanceRedistribution:
    """A source permits redistribution only with an approved REDISTRIBUTION purpose."""

    def __init__(self, governance: GovernanceFactory) -> None:
        self._governance = governance

    def allows(self, source_id: str, now: datetime) -> bool:
        return self._governance().can_fetch(source_id, Purpose.REDISTRIBUTION, now).allowed


def _status(scope: str, lookup: PolicyLookup[ResponsibleUsePolicy]) -> ResponsibleUseStatus:
    allowed = lookup.decision.allowed and lookup.policy is not None
    return ResponsibleUseStatus(
        account_scope=scope,
        allowed=allowed,
        reason=lookup.decision.reason.value,
        policy_version=lookup.decision.version,
    )


class GovernanceReadChecks:
    """Kill switches, source approval, payout policy and responsible use from F01.

    `actionability` returns the current F05 quote state for a record, or None when it is
    unknown. A BET additionally needs a fresh, unchanged quote. `governance` returns the
    service for the calling thread (see `per_thread`).
    """

    def __init__(
        self,
        governance: GovernanceFactory,
        actionability: ActionabilityLookup,
        purpose: Purpose = Purpose.PROTOTYPE,
    ) -> None:
        self._governance = governance
        self._actionability = actionability
        self._purpose = purpose

    def source_state(self, source_id: str, now: datetime) -> str | None:
        decision = self._governance().can_fetch(source_id, self._purpose, require_aware(now))
        return None if decision.allowed else decision.reason.value

    def responsible_use(self, account_scope: str, now: datetime) -> ResponsibleUseStatus:
        lookup = self._governance().get_responsible_use_policy(account_scope, require_aware(now))
        return _status(account_scope, lookup)

    def check(self, stored: StoredDecision, now: datetime) -> tuple[str, ...]:
        now = require_aware(now)
        record, context = stored.record, stored.context
        reasons: list[str] = []
        for source_id in context.source_ids:
            state = self.source_state(source_id, now)
            if state is not None:
                reasons.append(f"SOURCE:{source_id}:{state}")
        if record.bookmaker is None:
            reasons.append("BOOKMAKER_UNKNOWN")
        else:
            payout = self._governance().get_payout_policy(record.bookmaker, now, now)
            if not payout.decision.allowed:
                reasons.append(f"PAYOUT_POLICY:{payout.decision.reason.value}")
        responsible = self._governance().get_responsible_use_policy(context.account_scope, now)
        if not responsible.decision.allowed:
            reasons.append(f"RESPONSIBLE_USE:{responsible.decision.reason.value}")
        if record.status == RecommendationStatus.BET:
            problems = volatile_problems(
                record,
                now=now,
                actionability=self._actionability(stored, now),
                responsible_use=responsible,
            )
            reasons.extend(detail for _, detail in problems)
        return tuple(dict.fromkeys(reasons))


def history_actionability(
    history: QuoteHistory, policy: ActionabilityPolicy
) -> ActionabilityLookup:
    """Current F05 actionability from quote history. No quote key means unknown."""

    def lookup(stored: StoredDecision, now: datetime) -> Actionability | None:
        key = stored.context.quote_key
        if key is None:
            return None
        return history.actionability(key, at=now, policy=policy)

    return lookup


def source_health(
    checks: ReadChecks,
    observations: dict[str, datetime | None],
    now: datetime,
    stale_after_seconds: int,
) -> tuple[SourceHealth, ...]:
    """Per-source status. Only quote sources have a latest observation time."""
    rows = []
    for source_id in sorted(observations):
        latest = observations[source_id]
        age = int((now - latest).total_seconds()) if latest is not None else None
        try:
            state = checks.source_state(source_id, now)
        except Exception:  # noqa: BLE001 - a failed check must fail closed
            state = "CHECK_UNAVAILABLE"
        status: Literal["OK", "STALE", "DISABLED", "UNKNOWN"]
        if state is not None:
            status, reason = "DISABLED", state
        elif age is None:
            status, reason = "UNKNOWN", "NO_QUOTE_OBSERVATION_IN_DECISIONS"
        elif age > stale_after_seconds:
            status, reason = "STALE", f"LATEST_OBSERVATION_OLDER_THAN_{stale_after_seconds}S"
        else:
            status, reason = "OK", "ENABLED"
        rows.append(
            SourceHealth(
                source_id=source_id,
                status=status,
                reason=reason,
                latest_observation_at=latest,
                age_seconds=age,
            )
        )
    return tuple(rows)
