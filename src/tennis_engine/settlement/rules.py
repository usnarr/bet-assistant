"""F06 versioned payout, settlement and promotion rules with a fail-closed registry.

Rules state every semantic explicitly. A reviewed rule must set each semantic field,
even when the value is null, so a missing field is never read as "no tax" or "stands".
There are deliberately no tax or settlement defaults: unknown semantics stay unknown.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Protocol, Self

from pydantic import BeforeValidator, Field, TypeAdapter, model_validator

from tennis_engine.common.contracts import Contract, Currency, ExactDecimal, Identifier
from tennis_engine.contracts.domain import Market
from tennis_engine.governance.contracts import (
    Amount,
    EvidenceRef,
    Fraction,
    ReviewedPolicy,
    Text,
    utc,
)


def _reject_float(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("Use a decimal string or Decimal, never a float")
    return value


PositiveDecimal = Annotated[
    Decimal, BeforeValidator(_reject_float), Field(gt=0, allow_inf_nan=False)
]

ROUNDING_MODES = {
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_DOWN": ROUND_DOWN,
    "ROUND_UP": ROUND_UP,
}


class RuleReason(StrEnum):
    RULE_MISSING = "RULE_MISSING"
    RULE_KIND_MISMATCH = "RULE_KIND_MISMATCH"
    RULE_NOT_REVIEWED = "RULE_NOT_REVIEWED"
    RULE_SUSPENDED = "RULE_SUSPENDED"
    RULE_NOT_KNOWN_AT_DECISION = "RULE_NOT_KNOWN_AT_DECISION"
    RULE_REVIEW_EXPIRED = "RULE_REVIEW_EXPIRED"
    RULE_OUTSIDE_EFFECTIVE_INTERVAL = "RULE_OUTSIDE_EFFECTIVE_INTERVAL"
    RULE_EVIDENCE_PROBLEM = "RULE_EVIDENCE_PROBLEM"
    RULE_BOOKMAKER_MISMATCH = "RULE_BOOKMAKER_MISMATCH"


class RuleState(StrEnum):
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    SUSPENDED = "SUSPENDED"


class RuleRef(Contract):
    """Points to the archived document section that justifies one rule branch."""

    document_id: Identifier
    section: Text


class Rounding(Contract):
    quantum: PositiveDecimal
    mode: Literal["ROUND_HALF_UP", "ROUND_HALF_EVEN", "ROUND_DOWN", "ROUND_UP"]

    def apply(self, value: Decimal) -> Decimal:
        return value.quantize(self.quantum, rounding=ROUNDING_MODES[self.mode])


class StakeTax(Contract):
    """Tax deducted from the stake before odds apply: effective = S * (1 - rate)."""

    rate: Fraction
    rounding: Rounding
    rule: RuleRef


TaxBasis = Literal["gross_return", "gross_return_minus_stake", "gross_return_minus_effective_stake"]


def tax_basis(
    basis: TaxBasis, gross_return: Decimal, stake: Decimal, effective: Decimal
) -> Decimal:
    if basis == "gross_return":
        return gross_return
    if basis == "gross_return_minus_stake":
        return max(Decimal(0), gross_return - stake)
    return max(Decimal(0), gross_return - effective)


class WinningsTax(Contract):
    """Tax on a winning return when the threshold basis crosses the threshold."""

    threshold: Amount
    threshold_basis: TaxBasis
    comparison: Literal["gt", "ge"]
    rate: Fraction
    base: TaxBasis
    rounding: Rounding
    rule: RuleRef

    def applies(self, value: Decimal) -> bool:
        if self.comparison == "gt":
            return value > self.threshold
        return value >= self.threshold


class TaxRegime(Contract):
    # Both tax fields are required keys. JSON must state `null` to mean "none".
    stake_tax: StakeTax | None
    winnings_tax: WinningsTax | None
    return_rounding: Rounding


class RuleVersion(ReviewedPolicy):
    """A reviewed rule document version; `version` is globally unique in a registry."""

    kind: str
    state: RuleState = RuleState.DRAFT
    # Semantic fields that a REVIEWED rule must state explicitly, even as null.
    explicit_fields: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def reviewed_rules_are_complete(self) -> Self:
        if self.state == RuleState.REVIEWED:
            self.require_review()
            missing = [name for name in self.explicit_fields if name not in self.model_fields_set]
            if missing:
                raise ValueError(f"A reviewed rule must state: {', '.join(missing)}")
        return self

    @property
    def evidence_scope(self) -> str:
        return f"payout:rule:{self.version}"

    def problem(self, effective_at: datetime, known_at: datetime) -> RuleReason | None:
        effective_at, known_at = utc(effective_at), utc(known_at)
        if self.state == RuleState.SUSPENDED:
            return RuleReason.RULE_SUSPENDED
        if self.state != RuleState.REVIEWED:
            return RuleReason.RULE_NOT_REVIEWED
        if self.reviewed_at is None or self.reviewed_at > known_at:
            return RuleReason.RULE_NOT_KNOWN_AT_DECISION
        if self.review_due_at is None or known_at >= self.review_due_at:
            return RuleReason.RULE_REVIEW_EXPIRED
        if not self.contains(effective_at):
            return RuleReason.RULE_OUTSIDE_EFFECTIVE_INTERVAL
        return None


class JurisdictionPayoutRule(RuleVersion):
    kind: Literal["jurisdiction_payout"] = "jurisdiction_payout"
    jurisdiction: Literal["PL"] = "PL"
    taxes: TaxRegime | None = None
    explicit_fields: ClassVar[tuple[str, ...]] = ("taxes",)

    @model_validator(mode="after")
    def reviewed_taxes_known(self) -> Self:
        if self.state == RuleState.REVIEWED and self.taxes is None:
            raise ValueError("A reviewed jurisdiction rule requires a tax regime")
        return self


class BookmakerPayoutRule(RuleVersion):
    kind: Literal["bookmaker_payout"] = "bookmaker_payout"
    bookmaker: Identifier
    currency: Currency = Currency.PLN
    tax_treatment: Literal["jurisdiction", "bookmaker"] | None = None
    jurisdiction_rule_version: Identifier | None = None
    taxes: TaxRegime | None = None
    minimum_stake: Amount | None = None
    maximum_stake: Amount | None = None
    stake_increment: Amount | None = None
    maximum_cash_return: Amount | None = None
    explicit_fields: ClassVar[tuple[str, ...]] = (
        "tax_treatment",
        "jurisdiction_rule_version",
        "taxes",
        "minimum_stake",
        "maximum_stake",
        "stake_increment",
        "maximum_cash_return",
    )

    @model_validator(mode="after")
    def reviewed_payout_consistent(self) -> Self:
        if self.state != RuleState.REVIEWED:
            return self
        if self.tax_treatment is None or self.minimum_stake is None or not self.stake_increment:
            raise ValueError("A reviewed payout rule needs tax treatment, minimum and increment")
        if self.tax_treatment == "jurisdiction" and (
            self.jurisdiction_rule_version is None or self.taxes is not None
        ):
            raise ValueError("Jurisdiction treatment references exactly one jurisdiction rule")
        if self.tax_treatment == "bookmaker" and (
            self.taxes is None or self.jurisdiction_rule_version is not None
        ):
            raise ValueError("Bookmaker treatment states its own tax regime")
        if self.maximum_stake is not None and self.maximum_stake < self.minimum_stake:
            raise ValueError("Maximum stake cannot be below minimum stake")
        return self


class BranchAction(StrEnum):
    RESULT = "RESULT"  # Settle on the official winner; for a change branch: bet stands.
    VOID = "VOID"
    ADVANCING_PLAYER = "ADVANCING_PLAYER"
    REVIEW = "REVIEW"  # Manual review; the bet stays PENDING with evidence.


class Branch(Contract):
    action: BranchAction
    rule: RuleRef


class IncompleteMatchBranch(Contract):
    """Retirement, disqualification or abandonment after the match starts."""

    action: Literal[BranchAction.VOID, BranchAction.ADVANCING_PLAYER, BranchAction.REVIEW]
    # With ADVANCING_PLAYER: fewer completed sets than this makes the bet void.
    minimum_completed_sets: Annotated[int, Field(ge=0, le=3, strict=True)] | None
    rule: RuleRef


class PostponementBranch(Contract):
    # A match not completed within this window after the scheduled start is void.
    void_after_hours: Annotated[int, Field(gt=0, strict=True)]
    rule: RuleRef


class SettlementRule(RuleVersion):
    kind: Literal["bookmaker_settlement"] = "bookmaker_settlement"
    bookmaker: Identifier
    market: Market = Market.MATCH_WINNER
    void_cash_return: Literal["stake_deducted"] | None = None
    completed: Branch | None = None
    retirement: IncompleteMatchBranch | None = None
    walkover: Branch | None = None
    disqualification: IncompleteMatchBranch | None = None
    abandonment: IncompleteMatchBranch | None = None
    postponement: PostponementBranch | None = None
    venue_change: Branch | None = None
    surface_change: Branch | None = None
    format_change: Branch | None = None
    wrong_listing: Branch | None = None
    palpable_error: Branch | None = None
    explicit_fields: ClassVar[tuple[str, ...]] = (
        "void_cash_return",
        "completed",
        "retirement",
        "walkover",
        "disqualification",
        "abandonment",
        "postponement",
        "venue_change",
        "surface_change",
        "format_change",
        "wrong_listing",
        "palpable_error",
    )

    @model_validator(mode="after")
    def reviewed_settlement_consistent(self) -> Self:
        if self.state == RuleState.REVIEWED:
            if self.completed is None or self.completed.action != BranchAction.RESULT:
                raise ValueError("A reviewed settlement rule settles completed matches on result")
            if self.void_cash_return is None:
                raise ValueError("A reviewed settlement rule states the void cash return")
        return self


class PromotionKind(StrEnum):
    STAKE_TAX_COVERED = "STAKE_TAX_COVERED"
    ODDS_BOOST = "ODDS_BOOST"
    FREEBET = "FREEBET"
    EARLY_PAYOUT = "EARLY_PAYOUT"
    INSURANCE = "INSURANCE"
    CAPPED_BONUS = "CAPPED_BONUS"


# Only promotions with a complete executable outcome model can affect a payout.
SUPPORTED_PROMOTION_KINDS = frozenset({PromotionKind.STAKE_TAX_COVERED})


class PromotionRule(RuleVersion):
    kind: Literal["promotion"] = "promotion"
    promotion_id: Identifier
    bookmaker: Identifier
    promotion_kind: PromotionKind
    markets: tuple[Market, ...] = ()
    eligible_account_scopes: tuple[Identifier, ...] = ()
    minimum_odds: ExactDecimal | None = None
    maximum_stake: Amount | None = None
    rule: RuleRef | None = None
    explicit_fields: ClassVar[tuple[str, ...]] = (
        "markets",
        "eligible_account_scopes",
        "minimum_odds",
        "maximum_stake",
        "rule",
    )


AnyRule = Annotated[
    JurisdictionPayoutRule | BookmakerPayoutRule | SettlementRule | PromotionRule,
    Field(discriminator="kind"),
]
RULE_LIST = TypeAdapter(list[AnyRule])


class EvidenceChecker(Protocol):
    """Matches `GovernanceStore.evidence_problem`."""

    def __call__(
        self, scope: str, refs: tuple[EvidenceRef, ...], known_at: datetime
    ) -> str | None: ...


@dataclass(frozen=True)
class RuleLookup[R: RuleVersion]:
    rule: R | None = None
    reason: RuleReason | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.rule is not None and self.reason is None


class RuleRegistry:
    def __init__(
        self, rules: Iterable[RuleVersion], evidence_checker: EvidenceChecker | None = None
    ) -> None:
        self._rules: dict[str, RuleVersion] = {}
        for rule in rules:
            if rule.version in self._rules:
                raise ValueError(f"Duplicate rule version: {rule.version}")
            self._rules[rule.version] = rule
        self._evidence_checker = evidence_checker

    @classmethod
    def from_directory(
        cls, root: Path, evidence_checker: EvidenceChecker | None = None
    ) -> "RuleRegistry":
        rules: list[RuleVersion] = []
        for path in sorted(root.glob("*.json")):
            rules.extend(RULE_LIST.validate_python(json.loads(path.read_text("utf-8"))["rules"]))
        return cls(rules, evidence_checker)

    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    def lookup[R: RuleVersion](
        self,
        kind: type[R],
        version: str | None,
        *,
        effective_at: datetime,
        known_at: datetime,
        bookmaker: str | None = None,
    ) -> RuleLookup[R]:
        """Return a rule usable for a decision at `known_at` about a bet at `effective_at`."""
        rule = self._rules.get(version) if version else None
        if rule is None:
            return RuleLookup[R](reason=RuleReason.RULE_MISSING, detail=version)
        if not isinstance(rule, kind):
            return RuleLookup[R](reason=RuleReason.RULE_KIND_MISMATCH, detail=version)
        if bookmaker is not None and getattr(rule, "bookmaker", bookmaker) != bookmaker:
            return RuleLookup[R](reason=RuleReason.RULE_BOOKMAKER_MISMATCH, detail=version)
        problem = rule.problem(effective_at, known_at)
        if problem is not None:
            return RuleLookup[R](reason=problem, detail=version)
        if self._evidence_checker is not None:
            evidence = self._evidence_checker(rule.evidence_scope, rule.evidence, utc(known_at))
            if evidence is not None:
                return RuleLookup[R](reason=RuleReason.RULE_EVIDENCE_PROBLEM, detail=evidence)
        return RuleLookup[R](rule=rule)
