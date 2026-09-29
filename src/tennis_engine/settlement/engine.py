"""F06.4/F06.5 match-winner settlement from the bookmaker rule effective at bet time.

The result of each branch comes only from the reviewed rule. A missing rule, a missing
branch, a disputed result or an unknown advancing player keeps the bet PENDING.
"""

import hashlib
import json
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import (
    Contract,
    ExactDecimal,
    Identifier,
    Money,
    Timestamp,
)
from tennis_engine.contracts.domain import Market

from .rules import (
    Branch,
    BranchAction,
    IncompleteMatchBranch,
    RuleRegistry,
    SettlementRule,
)


class MatchStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    RETIRED = "RETIRED"
    WALKOVER = "WALKOVER"
    DISQUALIFIED = "DISQUALIFIED"
    ABANDONED = "ABANDONED"
    POSTPONED = "POSTPONED"
    CANCELLED = "CANCELLED"


class SettlementStatus(StrEnum):
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    PENDING = "PENDING"
    # Reserved for later handicap/total markets. Match winner never produces them.
    HALF_WON = "HALF_WON"
    HALF_LOST = "HALF_LOST"


FINAL_STATUSES = frozenset({SettlementStatus.WON, SettlementStatus.LOST, SettlementStatus.VOID})


class PendingReason(StrEnum):
    RULE_UNAVAILABLE = "RULE_UNAVAILABLE"
    RULE_BRANCH_MISSING = "RULE_BRANCH_MISSING"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    RESULT_DISPUTED = "RESULT_DISPUTED"
    MATCH_NOT_FINISHED = "MATCH_NOT_FINISHED"
    POSTPONEMENT_WINDOW_OPEN = "POSTPONEMENT_WINDOW_OPEN"
    WINNER_UNKNOWN = "WINNER_UNKNOWN"
    CANCELLATION_REVIEW = "CANCELLATION_REVIEW"


class SetScore(Contract):
    # Games in canonical `player_ids` order.
    games: tuple[Annotated[int, Field(ge=0, strict=True)], Annotated[int, Field(ge=0, strict=True)]]


class MatchOutcome(Contract):
    """Canonical sports truth used for settlement. Every change flag is stated explicitly."""

    match_id: UUID
    player_ids: tuple[UUID, UUID]
    status: MatchStatus
    scheduled_start: Timestamp
    completed_at: Timestamp | None
    # Official winner for COMPLETED; advancing player for retirement/walkover/DQ.
    winner_player_id: UUID | None
    completed_sets: Annotated[int, Field(ge=0, le=5, strict=True)]
    sets: tuple[SetScore, ...]
    postponed: bool
    venue_changed: bool
    surface_changed: bool
    format_changed: bool
    wrong_listing: bool
    palpable_error: bool
    disputed: bool
    observed_at: Timestamp
    evidence_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.player_ids[0] == self.player_ids[1]:
            raise ValueError("A match requires two distinct players")
        if self.winner_player_id is not None and self.winner_player_id not in self.player_ids:
            raise ValueError("The winner must be a match participant")
        if self.status == MatchStatus.COMPLETED and self.completed_at is None:
            raise ValueError("A completed match requires a completion time")
        return self


class SettlementContext(Contract):
    bet_id: UUID
    bookmaker: Identifier
    rule_version: Identifier
    market: Market = Market.MATCH_WINNER
    match_id: UUID
    selection_player_id: UUID
    stake: Money
    displayed_odds: Annotated[ExactDecimal, Field(gt=1)]
    cash_return_if_win: Money
    # Tax components are unknown (None) when the payout came only from a coupon preview.
    stake_tax: Money | None = None
    winnings_tax_if_win: Money | None = None
    bet_time: Timestamp
    settled_at: Timestamp  # Settlement decision time; rules must be known by then.
    outcome: MatchOutcome

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.outcome.match_id != self.match_id:
            raise ValueError("The outcome belongs to another match")
        if self.selection_player_id not in self.outcome.player_ids:
            raise ValueError("The selection must be a match participant")
        if self.cash_return_if_win.currency != self.stake.currency or self.stake.amount <= 0:
            raise ValueError("Settlement needs a positive stake in one currency")
        if self.outcome.observed_at > self.settled_at:
            raise ValueError("Settlement cannot use an outcome observed later")
        return self


class SettlementResult(Contract):
    bet_id: UUID
    status: SettlementStatus
    rule_version: Identifier
    stake_deducted: Money
    cash_return: Money | None
    net_profit: Money | None
    tax_amount: Money | None
    applied_rules: tuple[str, ...]
    evidence: tuple[Identifier, ...]
    pending_reason: PendingReason | None = None
    outcome_observed_at: Timestamp
    settled_at: Timestamp

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status in (SettlementStatus.HALF_WON, SettlementStatus.HALF_LOST):
            raise ValueError("Partial settlement is not enabled for any supported market")
        if self.status == SettlementStatus.PENDING:
            if self.pending_reason is None or self.cash_return is not None:
                raise ValueError("A pending result has a reason and no cash effect")
        elif self.pending_reason is not None or self.cash_return is None:
            raise ValueError("A final result has a cash return and no pending reason")
        if self.cash_return is not None and self.net_profit != Money(
            amount=self.cash_return.amount - self.stake_deducted.amount,
            currency=self.stake_deducted.currency,
        ):
            raise ValueError("net_profit must equal cash_return - stake_deducted")
        return self

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES

    def financial_digest(self) -> str:
        """Identity of the financial effect; timing and evidence refreshes do not change it."""
        data = {
            "bet_id": str(self.bet_id),
            "status": self.status.value,
            "rule_version": self.rule_version,
            "cash_return": None if self.cash_return is None else str(self.cash_return.amount),
            "pending_reason": self.pending_reason,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _label(rule: SettlementRule, branch: str, ref_section: str, document_id: str) -> str:
    return f"{rule.version}:{branch}:{document_id}:{ref_section}"


class _Pending(Exception):
    def __init__(self, reason: PendingReason, applied: str | None = None) -> None:
        self.reason = reason
        self.applied = applied


class _Void(Exception):
    def __init__(self, applied: str) -> None:
        self.applied = applied


def _change_branch(
    rule: SettlementRule, name: str, branch: Branch | None, applied: list[str]
) -> None:
    """A change or listing branch either lets the bet stand, voids it or holds it."""
    if branch is None:
        raise _Pending(PendingReason.RULE_BRANCH_MISSING, f"{rule.version}:{name}:missing")
    label = _label(rule, name, branch.rule.section, branch.rule.document_id)
    if branch.action == BranchAction.VOID:
        raise _Void(label)
    if branch.action in (BranchAction.REVIEW, BranchAction.ADVANCING_PLAYER):
        raise _Pending(PendingReason.MANUAL_REVIEW, label)
    applied.append(label)


def _winner_decides(context: SettlementContext, applied: list[str], label: str) -> SettlementStatus:
    winner = context.outcome.winner_player_id
    if winner is None:
        raise _Pending(PendingReason.WINNER_UNKNOWN, label)
    applied.append(label)
    return SettlementStatus.WON if winner == context.selection_player_id else SettlementStatus.LOST


def _incomplete(
    context: SettlementContext,
    rule: SettlementRule,
    name: str,
    branch: IncompleteMatchBranch | None,
    applied: list[str],
) -> SettlementStatus:
    if branch is None:
        raise _Pending(PendingReason.RULE_BRANCH_MISSING, f"{rule.version}:{name}:missing")
    label = _label(rule, name, branch.rule.section, branch.rule.document_id)
    if branch.action == BranchAction.VOID:
        raise _Void(label)
    if branch.action == BranchAction.REVIEW:
        raise _Pending(PendingReason.MANUAL_REVIEW, label)
    minimum = branch.minimum_completed_sets
    if minimum is not None and context.outcome.completed_sets < minimum:
        raise _Void(f"{label}:below_minimum_completed_sets")
    return _winner_decides(context, applied, label)


def _decide(
    context: SettlementContext, rule: SettlementRule, applied: list[str]
) -> SettlementStatus:
    outcome = context.outcome
    if outcome.disputed:
        raise _Pending(PendingReason.RESULT_DISPUTED)
    if outcome.palpable_error:
        _change_branch(rule, "palpable_error", rule.palpable_error, applied)
    if outcome.wrong_listing:
        _change_branch(rule, "wrong_listing", rule.wrong_listing, applied)
    for flag, name, branch in (
        (outcome.venue_changed, "venue_change", rule.venue_change),
        (outcome.surface_changed, "surface_change", rule.surface_change),
        (outcome.format_changed, "format_change", rule.format_change),
    ):
        if flag:
            _change_branch(rule, name, branch, applied)

    status = outcome.status
    if status in (MatchStatus.NOT_STARTED, MatchStatus.IN_PROGRESS, MatchStatus.SUSPENDED):
        raise _Pending(PendingReason.MATCH_NOT_FINISHED)
    if status == MatchStatus.CANCELLED:
        raise _Pending(PendingReason.CANCELLATION_REVIEW)
    if outcome.postponed or status == MatchStatus.POSTPONED:
        postponement = rule.postponement
        if postponement is None:
            raise _Pending(
                PendingReason.RULE_BRANCH_MISSING, f"{rule.version}:postponement:missing"
            )
        label = _label(
            rule, "postponement", postponement.rule.section, postponement.rule.document_id
        )
        deadline = outcome.scheduled_start + timedelta(hours=postponement.void_after_hours)
        finished_at = outcome.completed_at if status != MatchStatus.POSTPONED else None
        if finished_at is None or finished_at >= deadline:
            if context.settled_at >= deadline:
                raise _Void(label)
            raise _Pending(PendingReason.POSTPONEMENT_WINDOW_OPEN, label)
        applied.append(label)
    if status == MatchStatus.COMPLETED:
        assert rule.completed is not None  # Guaranteed for reviewed rules.
        return _winner_decides(
            context,
            applied,
            _label(rule, "completed", rule.completed.rule.section, rule.completed.rule.document_id),
        )
    if status == MatchStatus.WALKOVER:
        walkover = rule.walkover
        if walkover is None:
            raise _Pending(PendingReason.RULE_BRANCH_MISSING, f"{rule.version}:walkover:missing")
        label = _label(rule, "walkover", walkover.rule.section, walkover.rule.document_id)
        if walkover.action == BranchAction.VOID:
            raise _Void(label)
        if walkover.action == BranchAction.REVIEW:
            raise _Pending(PendingReason.MANUAL_REVIEW, label)
        return _winner_decides(context, applied, label)
    if status == MatchStatus.RETIRED:
        return _incomplete(context, rule, "retirement", rule.retirement, applied)
    if status == MatchStatus.DISQUALIFIED:
        return _incomplete(context, rule, "disqualification", rule.disqualification, applied)
    if status == MatchStatus.ABANDONED:
        return _incomplete(context, rule, "abandonment", rule.abandonment, applied)
    # POSTPONED is fully handled above; this keeps the mapping exhaustive.
    raise _Pending(PendingReason.MATCH_NOT_FINISHED)


def settle(context: SettlementContext, registry: RuleRegistry) -> SettlementResult:
    """Settle one match-winner bet. Repeated calls with the same inputs give the same result."""
    stake = context.stake
    zero = Money(amount=stake.amount - stake.amount, currency=stake.currency)
    applied: list[str] = []
    common = {
        "bet_id": context.bet_id,
        "rule_version": context.rule_version,
        "stake_deducted": stake,
        "evidence": context.outcome.evidence_ids,
        "outcome_observed_at": context.outcome.observed_at,
        "settled_at": context.settled_at,
    }

    def pending(reason: PendingReason, label: str | None = None) -> SettlementResult:
        return SettlementResult(
            status=SettlementStatus.PENDING,
            cash_return=None,
            net_profit=None,
            tax_amount=None,
            applied_rules=tuple(applied + ([label] if label else [])),
            pending_reason=reason,
            **common,
        )

    lookup = registry.lookup(
        SettlementRule,
        context.rule_version,
        effective_at=context.bet_time,
        known_at=context.settled_at,
        bookmaker=context.bookmaker,
    )
    rule = lookup.rule
    if rule is None or rule.market != context.market:
        return pending(PendingReason.RULE_UNAVAILABLE, f"{context.rule_version}:{lookup.reason}")
    try:
        status = _decide(context, rule, applied)
    except _Pending as held:
        return pending(held.reason, held.applied)
    except _Void as voided:
        applied.append(voided.applied)
        # Reviewed rules state the void return; only the full deducted stake is supported.
        return SettlementResult(
            status=SettlementStatus.VOID,
            cash_return=stake,
            net_profit=zero,
            tax_amount=zero,
            applied_rules=tuple(applied),
            **common,
        )
    won = status == SettlementStatus.WON
    cash = context.cash_return_if_win if won else zero
    tax = context.stake_tax
    if won and tax is not None:
        winnings_tax = context.winnings_tax_if_win
        tax = (
            None
            if winnings_tax is None
            else Money(amount=tax.amount + winnings_tax.amount, currency=tax.currency)
        )
    return SettlementResult(
        status=status,
        cash_return=cash,
        net_profit=Money(amount=cash.amount - stake.amount, currency=stake.currency),
        tax_amount=tax,
        applied_rules=tuple(applied),
        **common,
    )
