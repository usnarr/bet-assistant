"""Fail-closed policy lookups, for execution-time fetch/publication guards."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from zoneinfo import ZoneInfo

from .contracts import (
    Decision,
    Lifecycle,
    PayoutPolicy,
    PayoutSchedule,
    Purpose,
    ResponsibleUsePolicy,
    ResponsibleUseSchedule,
    ReviewedPolicy,
    SourcePolicy,
    utc,
)
from .store import GovernanceStore

T = TypeVar("T")


@dataclass(frozen=True)
class PolicyLookup[T]:
    decision: Decision
    policy: T | None = None


class PermissionDenied(PermissionError):
    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__(decision.reason)


class GovernanceService:
    def __init__(self, store: GovernanceStore):
        self.store = store

    def _review_problem(self, scope: str, policy: ReviewedPolicy, known_at: datetime) -> str | None:
        if not policy.reviewed_at or policy.reviewed_at > known_at:
            return "POLICY_NOT_REVIEWED"
        if not policy.review_due_at or known_at >= policy.review_due_at:
            return "REVIEW_EXPIRED"
        return self.store.evidence_problem(scope, policy.evidence, known_at)

    def can_fetch(self, source_id: str, purpose: Purpose, now: datetime | None = None) -> Decision:
        # Validate purpose even for callers that bypass Python type hints.
        purpose = Purpose(purpose)
        now = utc(now if now is not None else self.store.clock())
        row = self.store.latest("source", source_id, now)
        if row is None:
            return Decision.deny("SOURCE_UNKNOWN")
        policy = SourcePolicy.model_validate_json(row["payload"])
        reason = None
        if policy.kill_switch:
            reason = "SOURCE_DISABLED"
        elif policy.state == Lifecycle.SUSPENDED:
            reason = "SOURCE_SUSPENDED"
        elif policy.state == Lifecycle.DRAFT:
            reason = "SOURCE_NOT_APPROVED"
        elif not policy.contains(now):
            reason = "SOURCE_OUTSIDE_EFFECTIVE_INTERVAL"
        elif purpose != Purpose.PROTOTYPE and policy.state != Lifecycle.PRODUCTION_APPROVED:
            reason = "PRODUCTION_APPROVAL_REQUIRED"
        elif purpose not in policy.allowed_purposes:
            reason = "PURPOSE_NOT_ALLOWED"
        else:
            reason = self._review_problem(f"source:{source_id}", policy, now)
        if reason:
            return Decision.deny(reason, policy.version, row["revision"])
        return Decision(allowed=True, version=policy.version, revision=row["revision"])

    def execute_fetch(self, source_id: str, purpose: Purpose, fetch: Callable[[], T]) -> T:
        """Queued jobs call this when executed, never reuse an earlier allow result."""
        decision = self.can_fetch(source_id, purpose)
        if not decision.allowed:
            raise PermissionDenied(decision)
        return fetch()

    def get_payout_policy(
        self, bookmaker: str, effective_at: datetime, known_at: datetime
    ) -> PolicyLookup[PayoutPolicy]:
        effective_at, known_at = utc(effective_at), utc(known_at)
        row = self.store.latest("payout", bookmaker, known_at)
        if row is None:
            return PolicyLookup(Decision.deny("PAYOUT_POLICY_MISSING"))
        schedule = PayoutSchedule.model_validate_json(row["payload"])
        for policy in schedule.policies:
            if policy.contains(effective_at):
                reason = (
                    "PAYOUT_POLICY_NOT_APPROVED"
                    if policy.state != "APPROVED"
                    else self._review_problem(f"payout:{bookmaker}", policy, known_at)
                )
                if reason:
                    return PolicyLookup(Decision.deny(reason, policy.version, row["revision"]))
                return PolicyLookup(
                    Decision(allowed=True, version=policy.version, revision=row["revision"]),
                    policy,
                )
        return PolicyLookup(Decision.deny("PAYOUT_POLICY_OUTSIDE_EFFECTIVE_INTERVAL"))

    def get_responsible_use_policy(
        self, account_scope: str, now: datetime | None = None
    ) -> PolicyLookup[ResponsibleUsePolicy]:
        now = utc(now if now is not None else self.store.clock())
        if self.store.global_disabled(now):
            return PolicyLookup(Decision.deny("GLOBAL_DISABLE"))
        row = self.store.latest("responsible_use", account_scope, now)
        if row is None:
            return PolicyLookup(Decision.deny("RESPONSIBLE_USE_POLICY_MISSING"))
        schedule = ResponsibleUseSchedule.model_validate_json(row["payload"])
        for policy in schedule.policies:
            if not policy.contains(now):
                continue
            reason = None
            if policy.state != "APPROVED":
                reason = "RESPONSIBLE_USE_POLICY_NOT_APPROVED"
            elif policy.disable_recommendations:
                reason = "ACCOUNT_DISABLED"
            elif policy.cooling_off_until and now < policy.cooling_off_until:
                reason = "COOLING_OFF"
            else:
                reason = self._review_problem(f"responsible_use:{account_scope}", policy, now)
            if reason:
                return PolicyLookup(Decision.deny(reason, policy.version, row["revision"]))
            return PolicyLookup(
                Decision(allowed=True, version=policy.version, revision=row["revision"]),
                policy,
            )
        return PolicyLookup(Decision.deny("RESPONSIBLE_USE_POLICY_OUTSIDE_EFFECTIVE_INTERVAL"))

    def publication_gate(
        self, source_ids: Iterable[str], bookmaker: str, account_scope: str, purpose: Purpose
    ) -> Decision:
        """F06/F12 must additionally enforce payout economics and transactional limits."""
        now = utc(self.store.clock())
        sources = tuple(source_ids)
        if not sources:
            return Decision.deny("SOURCE_LINEAGE_MISSING")
        for source_id in sources:
            access = self.can_fetch(source_id, purpose, now)
            if not access.allowed:
                return access
        payout = self.get_payout_policy(bookmaker, now, now)
        if not payout.decision.allowed:
            return payout.decision
        return self.get_responsible_use_policy(account_scope, now).decision


def reset_period(now: datetime, period: str) -> tuple[datetime, datetime]:
    """Calendar boundaries in Warsaw; ISO weeks start Monday. Return UTC bounds."""
    local = utc(now).astimezone(ZoneInfo("Europe/Warsaw"))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        end = start + timedelta(days=1)
    elif period == "weekly":
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    elif period == "monthly":
        start = start.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        raise ValueError("Expected daily, weekly or monthly period")
    return start.astimezone(UTC), end.astimezone(UTC)
