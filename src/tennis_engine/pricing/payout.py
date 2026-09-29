"""F06.2, F06.3, F06.7, F06.8: exact payout resolution with coupon-preview precedence.

Order: a bound coupon preview, then the reviewed bookmaker rule, then the reviewed
jurisdiction rule it references. Plain `stake * odds` is a research diagnostic only and
can never make a payout actionable. `W` is the cash returned on a win, including stake.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, ExactDecimal, Identifier, Money, Timestamp
from tennis_engine.contracts.domain import Market
from tennis_engine.governance.contracts import EvidenceRef, PayoutPolicy
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.settlement.rules import (
    SUPPORTED_PROMOTION_KINDS,
    BookmakerPayoutRule,
    JurisdictionPayoutRule,
    PromotionKind,
    PromotionRule,
    RuleRegistry,
    TaxRegime,
    tax_basis,
)

CENT = Decimal("0.01")
Odds = Annotated[ExactDecimal, Field(gt=1)]


class PayoutSource(StrEnum):
    COUPON_PREVIEW = "COUPON_PREVIEW"
    BOOKMAKER_RULE = "BOOKMAKER_RULE"
    JURISDICTION_RULE = "JURISDICTION_RULE"


class PayoutReason(StrEnum):
    PAYOUT_POLICY_UNAVAILABLE = "PAYOUT_POLICY_UNAVAILABLE"
    PAYOUT_RULE_UNAVAILABLE = "PAYOUT_RULE_UNAVAILABLE"
    JURISDICTION_RULE_UNAVAILABLE = "JURISDICTION_RULE_UNAVAILABLE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    STAKE_BELOW_MINIMUM = "STAKE_BELOW_MINIMUM"
    STAKE_ABOVE_MAXIMUM = "STAKE_ABOVE_MAXIMUM"
    STAKE_NOT_ON_INCREMENT = "STAKE_NOT_ON_INCREMENT"
    PROMOTION_DISABLED = "PROMOTION_DISABLED"
    PROMOTION_RULE_UNAVAILABLE = "PROMOTION_RULE_UNAVAILABLE"
    PROMOTION_UNSUPPORTED = "PROMOTION_UNSUPPORTED"
    PROMOTION_NOT_ELIGIBLE = "PROMOTION_NOT_ELIGIBLE"
    RETURN_NOT_CENT_EXACT = "RETURN_NOT_CENT_EXACT"
    PREVIEW_MISMATCH = "PREVIEW_MISMATCH"


class PreviewBindingProblem(StrEnum):
    BOOKMAKER = "BOOKMAKER"
    MARKET = "MARKET"
    SELECTION = "SELECTION"
    ODDS = "ODDS"
    STAKE = "STAKE"
    PROMOTION = "PROMOTION"
    ACCOUNT_SCOPE = "ACCOUNT_SCOPE"
    POLICY = "POLICY"
    NOT_YET_CAPTURED = "NOT_YET_CAPTURED"
    EXPIRED = "EXPIRED"


class PayoutRequest(Contract):
    bookmaker: Identifier
    market: Market = Market.MATCH_WINNER
    selection_key: Identifier
    decimal_odds: Odds
    stake: Money
    account_scope: Identifier
    promotion_rule_version: Identifier | None = None
    bet_time: Timestamp  # When the bet is (or would be) struck: selects effective rules.
    known_at: Timestamp  # Decision time: only rules/previews known then may be used.

    @model_validator(mode="after")
    def positive_stake(self) -> Self:
        if self.stake.amount <= 0:
            raise ValueError("A payout request requires a positive stake")
        return self


class CouponPreview(Contract):
    """A bookmaker coupon preview, valid only for the exact inputs it was captured with."""

    preview_id: UUID
    bookmaker: Identifier
    market: Market = Market.MATCH_WINNER
    selection_key: Identifier
    decimal_odds: Odds
    stake: Money
    account_scope: Identifier
    promotion_rule_version: Identifier | None = None
    payout_policy_version: Identifier
    captured_at: Timestamp
    expires_at: Timestamp
    cash_return_if_win: Money
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.expires_at <= self.captured_at:
            raise ValueError("A preview must expire after capture")
        if self.cash_return_if_win.currency != self.stake.currency:
            raise ValueError("Preview currencies must match")
        return self

    def binding_problems(
        self, request: PayoutRequest, policy_version: str
    ) -> tuple[PreviewBindingProblem, ...]:
        checks = (
            (self.bookmaker == request.bookmaker, PreviewBindingProblem.BOOKMAKER),
            (self.market == request.market, PreviewBindingProblem.MARKET),
            (self.selection_key == request.selection_key, PreviewBindingProblem.SELECTION),
            (self.decimal_odds == request.decimal_odds, PreviewBindingProblem.ODDS),
            (self.stake == request.stake, PreviewBindingProblem.STAKE),
            (
                self.promotion_rule_version == request.promotion_rule_version,
                PreviewBindingProblem.PROMOTION,
            ),
            (self.account_scope == request.account_scope, PreviewBindingProblem.ACCOUNT_SCOPE),
            (self.payout_policy_version == policy_version, PreviewBindingProblem.POLICY),
            (self.captured_at <= request.known_at, PreviewBindingProblem.NOT_YET_CAPTURED),
            (request.known_at < self.expires_at, PreviewBindingProblem.EXPIRED),
        )
        return tuple(problem for passed, problem in checks if not passed)


class PayoutBreakdown(Contract):
    stake: Money
    stake_tax: Money
    effective_stake: Money
    gross_return: Money
    winnings_tax: Money
    cap_reduction: Money
    cash_return_if_win: Money
    applied_rules: tuple[str, ...]


class Reconciliation(StrEnum):
    MATCHED = "MATCHED"
    MISMATCHED = "MISMATCHED"
    CALCULATOR_UNAVAILABLE = "CALCULATOR_UNAVAILABLE"


class PayoutResolution(Contract):
    actionable: bool
    source: PayoutSource | None = None
    cash_return_if_win: Money | None = None
    cash_return_if_loss: Money = Money(amount=Decimal("0.00"))
    breakdown: PayoutBreakdown | None = None
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    policy_version: Identifier | None = None
    rule_versions: tuple[Identifier, ...] = ()
    preview_id: UUID | None = None
    reconciliation: Reconciliation | None = None
    review_required: bool = False
    # Generic `stake * odds`, for research diagnostics only. Never authorizes a bet.
    research_cash_return: Money | None = None

    @model_validator(mode="after")
    def actionable_is_complete(self) -> Self:
        if self.actionable and (
            self.reasons or self.cash_return_if_win is None or self.source is None
        ):
            raise ValueError("An actionable payout needs a source, a return and no failures")
        if not self.actionable and not self.reasons:
            raise ValueError("A non-actionable payout requires a reason")
        return self


def _money(value: Decimal, currency_from: Money) -> Money:
    return Money(amount=value, currency=currency_from.currency)


def calculate_payout(
    taxes: TaxRegime,
    stake: Money,
    decimal_odds: Decimal,
    *,
    maximum_cash_return: Decimal | None = None,
    stake_tax_waived: bool = False,
    rule_labels: tuple[str, ...] = (),
) -> PayoutBreakdown:
    """Apply one reviewed tax regime exactly, rounding only where the rule says so."""
    applied = list(rule_labels)
    amount = stake.amount
    effective = amount
    if taxes.stake_tax is not None and not stake_tax_waived:
        effective = taxes.stake_tax.rounding.apply(amount * (1 - taxes.stake_tax.rate))
        applied.append(
            f"stake_tax:{taxes.stake_tax.rule.document_id}:{taxes.stake_tax.rule.section}"
        )
    gross = taxes.return_rounding.apply(effective * decimal_odds)
    winnings_tax = Decimal("0.00")
    tax = taxes.winnings_tax
    if tax is not None and tax.applies(tax_basis(tax.threshold_basis, gross, amount, effective)):
        winnings_tax = tax.rounding.apply(tax_basis(tax.base, gross, amount, effective) * tax.rate)
        applied.append(f"winnings_tax:{tax.rule.document_id}:{tax.rule.section}")
    cash = gross - winnings_tax
    cap_reduction = Decimal("0.00")
    if maximum_cash_return is not None and cash > maximum_cash_return:
        cap_reduction = cash - maximum_cash_return
        cash = maximum_cash_return
        applied.append("maximum_cash_return")
    for value in (effective, gross, winnings_tax, cash):
        if value != value.quantize(CENT):
            raise ArithmeticError(PayoutReason.RETURN_NOT_CENT_EXACT)
    return PayoutBreakdown(
        stake=stake,
        stake_tax=_money((amount - effective).quantize(CENT), stake),
        effective_stake=_money(effective.quantize(CENT), stake),
        gross_return=_money(gross.quantize(CENT), stake),
        winnings_tax=_money(winnings_tax.quantize(CENT), stake),
        cap_reduction=_money(cap_reduction.quantize(CENT), stake),
        cash_return_if_win=_money(cash.quantize(CENT), stake),
        applied_rules=tuple(applied),
    )


def _stake_problem(rule: BookmakerPayoutRule, stake: Decimal) -> PayoutReason | None:
    if rule.minimum_stake is None or stake < rule.minimum_stake:
        return PayoutReason.STAKE_BELOW_MINIMUM
    if rule.maximum_stake is not None and stake > rule.maximum_stake:
        return PayoutReason.STAKE_ABOVE_MAXIMUM
    if rule.stake_increment is None or (stake - rule.minimum_stake) % rule.stake_increment:
        return PayoutReason.STAKE_NOT_ON_INCREMENT
    return None


def _promotion_problem(promotion: PromotionRule, request: PayoutRequest) -> PayoutReason | None:
    if promotion.promotion_kind not in SUPPORTED_PROMOTION_KINDS:
        return PayoutReason.PROMOTION_UNSUPPORTED
    eligible = (
        promotion.bookmaker == request.bookmaker
        and request.market in promotion.markets
        and request.account_scope in promotion.eligible_account_scopes
        and (promotion.minimum_odds is None or request.decimal_odds >= promotion.minimum_odds)
        and (promotion.maximum_stake is None or request.stake.amount <= promotion.maximum_stake)
    )
    return None if eligible else PayoutReason.PROMOTION_NOT_ELIGIBLE


def resolve_payout(
    request: PayoutRequest,
    policy_lookup: PolicyLookup[PayoutPolicy],
    registry: RuleRegistry,
    preview: CouponPreview | None = None,
) -> PayoutResolution:
    """Resolve `W` for one exact request. Every unknown makes the payout non-actionable."""
    research = _money((request.stake.amount * request.decimal_odds).quantize(CENT), request.stake)
    policy = policy_lookup.policy
    if not policy_lookup.decision.allowed or policy is None:
        return PayoutResolution(
            actionable=False,
            reasons=(
                PayoutReason.PAYOUT_POLICY_UNAVAILABLE,
                str(policy_lookup.decision.reason),
            ),
            policy_version=policy_lookup.decision.version,
            research_cash_return=research,
        )
    if policy.bookmaker != request.bookmaker:
        return PayoutResolution(
            actionable=False,
            reasons=(PayoutReason.PAYOUT_POLICY_UNAVAILABLE, "PAYOUT_POLICY_BOOKMAKER_MISMATCH"),
            policy_version=policy.version,
            research_cash_return=research,
        )

    reasons: list[str] = []
    notes: list[str] = []
    versions: list[str] = []
    breakdown: PayoutBreakdown | None = None
    source: PayoutSource | None = None
    effective_at, known_at = request.bet_time, request.known_at

    stake_tax_waived = False
    if request.promotion_rule_version is not None:
        if policy.promotion_handling != "reviewed_rules":
            reasons.append(PayoutReason.PROMOTION_DISABLED)
        else:
            promotion = registry.lookup(
                PromotionRule,
                request.promotion_rule_version,
                bookmaker=request.bookmaker,
                effective_at=effective_at,
                known_at=known_at,
            )
            if promotion.rule is None:
                reasons += [PayoutReason.PROMOTION_RULE_UNAVAILABLE, str(promotion.reason)]
            else:
                versions.append(promotion.rule.version)
                problem = _promotion_problem(promotion.rule, request)
                if problem is not None:
                    reasons.append(problem)
                stake_tax_waived = promotion.rule.promotion_kind == PromotionKind.STAKE_TAX_COVERED

    bookmaker_rule = registry.lookup(
        BookmakerPayoutRule,
        policy.payout_rule_version,
        bookmaker=request.bookmaker,
        effective_at=effective_at,
        known_at=known_at,
    )
    calculator_reasons: list[str] = []
    rule = bookmaker_rule.rule
    if rule is None:
        calculator_reasons += [PayoutReason.PAYOUT_RULE_UNAVAILABLE, str(bookmaker_rule.reason)]
    else:
        versions.append(rule.version)
        if rule.currency != request.stake.currency:
            calculator_reasons.append(PayoutReason.CURRENCY_MISMATCH)
        stake_problem = _stake_problem(rule, request.stake.amount)
        if stake_problem is not None:
            calculator_reasons.append(stake_problem)
        taxes: TaxRegime | None = rule.taxes
        source = PayoutSource.BOOKMAKER_RULE
        if rule.tax_treatment == "jurisdiction":
            jurisdiction = registry.lookup(
                JurisdictionPayoutRule,
                rule.jurisdiction_rule_version,
                effective_at=effective_at,
                known_at=known_at,
            )
            taxes = jurisdiction.rule.taxes if jurisdiction.rule is not None else None
            source = PayoutSource.JURISDICTION_RULE
            if jurisdiction.rule is None:
                calculator_reasons += [
                    PayoutReason.JURISDICTION_RULE_UNAVAILABLE,
                    str(jurisdiction.reason),
                ]
            else:
                versions.append(jurisdiction.rule.version)
        if taxes is not None and not calculator_reasons:
            try:
                breakdown = calculate_payout(
                    taxes,
                    request.stake,
                    request.decimal_odds,
                    maximum_cash_return=rule.maximum_cash_return,
                    stake_tax_waived=stake_tax_waived,
                    rule_labels=(f"payout_rule:{rule.version}",),
                )
            except ArithmeticError:
                calculator_reasons.append(PayoutReason.RETURN_NOT_CENT_EXACT)

    bound_preview = None
    if preview is not None:
        problems = preview.binding_problems(request, policy.version)
        if problems:
            notes += [f"PREVIEW_NOT_BOUND:{problem}" for problem in problems]
        else:
            bound_preview = preview

    if bound_preview is None:
        reasons += calculator_reasons
        if breakdown is None and not reasons:
            reasons.append(PayoutReason.PAYOUT_RULE_UNAVAILABLE)
        return PayoutResolution(
            actionable=not reasons,
            source=source if not reasons else None,
            cash_return_if_win=breakdown.cash_return_if_win if breakdown and not reasons else None,
            breakdown=breakdown,
            reasons=tuple(reasons),
            notes=tuple(notes),
            policy_version=policy.version,
            rule_versions=tuple(versions),
            research_cash_return=research,
        )

    # A bound preview is the preferred input. The calculator can still veto it.
    if breakdown is None:
        reconciliation = Reconciliation.CALCULATOR_UNAVAILABLE
        notes += [f"CALCULATOR:{reason}" for reason in calculator_reasons]
    elif breakdown.cash_return_if_win == bound_preview.cash_return_if_win:
        reconciliation = Reconciliation.MATCHED
    else:
        reconciliation = Reconciliation.MISMATCHED
        reasons.append(PayoutReason.PREVIEW_MISMATCH)
    return PayoutResolution(
        actionable=not reasons,
        source=PayoutSource.COUPON_PREVIEW if not reasons else None,
        cash_return_if_win=bound_preview.cash_return_if_win if not reasons else None,
        breakdown=breakdown,
        reasons=tuple(reasons),
        notes=tuple(notes),
        policy_version=policy.version,
        rule_versions=tuple(versions),
        preview_id=bound_preview.preview_id,
        reconciliation=reconciliation,
        review_required=reconciliation == Reconciliation.MISMATCHED,
        research_cash_return=research,
    )
