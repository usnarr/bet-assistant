"""F12.2, F12.3, F12.8 ordered decision gates and BET, WATCH or NO_BET records.

Every gate in blueprint section 28 runs and is recorded, including after a failure, so
the record lists every failed reason. A missing input fails its gate; it never passes.
Deterministic services own every number here; an agent or operator cannot override a
gate. A record is virtual; nothing in this module places a bet.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import model_validator

from tennis_engine.common.clock import require_aware
from tennis_engine.common.contracts import (
    Contract,
    Digest,
    Identifier,
    Money,
    Probability,
    ReasonCode,
    Timestamp,
    VersionRef,
)
from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import (
    AuditLineage,
    GateResult,
    Market,
    Recommendation,
    RecommendationStatus,
)
from tennis_engine.features.quality import QualityReport
from tennis_engine.governance.contracts import Decision, ResponsibleUsePolicy
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.ingestion.bookmakers.contracts import CanonicalQuote
from tennis_engine.ingestion.bookmakers.quotes import Actionability, ActionabilityReason
from tennis_engine.normalization.contracts import MatchResolution
from tennis_engine.settlement.rules import RuleLookup, SettlementRule

from .payout import PayoutResolution
from .risk import Capacity, DecisionPolicy, ExposureState, StakeRules, capacity, size_stake
from .value import BinaryValue, binary_value

CENT = Decimal("0.01")


class Gate(StrEnum):
    DECISION_POLICY = "decision_policy"
    IDENTITY = "identity_resolved"
    NOT_STARTED = "match_not_started"
    QUOTE_FRESH = "quote_fresh"
    MARKET = "market_and_selection_mapped"
    RULES = "rules_available"
    QUALITY = "feature_quality"
    MODEL_DOMAIN = "model_supported_domain"
    MODEL_CALIBRATED = "model_calibrated"
    MODEL_DISAGREEMENT = "model_disagreement"
    CENTRAL_EV = "central_ev_positive"
    CONSERVATIVE_EV = "conservative_ev_positive"
    CONSERVATIVE_ROI = "conservative_roi_threshold"
    OUTLIER = "odds_outlier_confirmed"
    RISK_BUDGET = "risk_budget_available"
    RESPONSIBLE_USE = "responsible_use_available"
    STAKE = "stake_sized"


VALUE_GATES = frozenset({Gate.CENTRAL_EV, Gate.CONSERVATIVE_EV, Gate.CONSERVATIVE_ROI})


class ModelAssessment(Contract):
    """Model output for the selected player, supplied by F09/F11."""

    model: VersionRef
    probability: Probability
    conservative_probability: Probability
    # SETTLED_WIN already includes void/retirement outcomes; SPORTING_WIN does not.
    semantics: Literal["SETTLED_WIN", "SPORTING_WIN"]
    in_supported_domain: bool | None
    calibrated: bool | None
    disagreement: Probability | None
    generated_at: Timestamp
    feature_vector_sha256: Digest

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.conservative_probability > self.probability:
            raise ValueError("The conservative probability cannot exceed the central one")
        return self


@dataclass(frozen=True)
class DecisionInputs:
    """Everything one decision reads. Callers pass what they know; None means unknown."""

    decision_key: str
    decided_at: datetime
    ledger_id: str
    quote: CanonicalQuote | None
    event_resolution: MatchResolution | None
    actionability: Actionability | None
    settlement_rule: RuleLookup[SettlementRule]
    payout: Callable[[Money], PayoutResolution]
    stake_rules: StakeRules | None
    quality: QualityReport | None
    model: ModelAssessment | None
    consensus_probability: Decimal | None
    publication: Decision
    responsible_use: PolicyLookup[ResponsibleUsePolicy]
    decision_policy: DecisionPolicy | None
    exposure: ExposureState | None
    lineage: AuditLineage


class GateOutcome(Contract):
    gate: Gate
    passed: bool
    code: ReasonCode | None = None
    detail: tuple[str, ...] = ()

    def to_result(self) -> GateResult:
        return GateResult(
            gate=self.gate.value,
            passed=self.passed,
            reason=self.code,
            evidence_ids=tuple(_evidence_id(item) for item in self.detail),
        )


def _evidence_id(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "_.:-" else "-" for ch in text.lower())
    return cleaned.strip("-_.:") or "detail"


class ValueSummary(Contract):
    stake: Money
    cash_return_if_win: Money
    break_even_probability: Decimal
    expected_value: Decimal
    expected_roi: Decimal
    conservative_expected_value: Decimal
    conservative_roi: Decimal
    probability_edge: Decimal


class DecisionRecord(Contract):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: UUID
    decision_key: str
    version: int = 1
    supersedes: UUID | None = None
    status: RecommendationStatus
    decided_at: Timestamp
    expires_at: Timestamp
    ledger_id: Identifier
    match_id: UUID | None
    selection_player_id: UUID | None
    bookmaker: Identifier | None
    quote_id: UUID | None
    decimal_odds: Decimal | None
    stake: Money
    central_probability: Probability | None
    conservative_probability: Probability | None
    probability_semantics: str | None
    value: ValueSummary | None
    capacity: Capacity | None
    gates: tuple[GateOutcome, ...]
    failed_gates: tuple[Gate, ...]
    payout_source: str | None
    policy_versions: tuple[str, ...]
    lineage: AuditLineage
    virtual: Literal[True] = True

    @model_validator(mode="after")
    def invariants(self) -> Self:
        if self.status != RecommendationStatus.BET and self.stake.amount != 0:
            raise ValueError("WATCH and NO_BET have no positive stake")
        if self.status == RecommendationStatus.BET and (
            self.failed_gates or self.stake.amount <= 0
        ):
            raise ValueError("BET requires every gate to pass and a positive stake")
        if self.status == RecommendationStatus.NO_BET and not self.failed_gates:
            raise ValueError("NO_BET requires a failed gate")
        if self.status == RecommendationStatus.WATCH and (
            not self.failed_gates or set(self.failed_gates) - VALUE_GATES - {Gate.STAKE}
        ):
            raise ValueError("WATCH requires only value gates to fail")
        if self.expires_at <= self.decided_at:
            raise ValueError("A decision must expire after it is made")
        return self

    def to_recommendation(self) -> Recommendation:
        """Domain recommendation for F14. EV is floored to whole grosze for display."""
        ev = self.value.expected_value if self.value is not None else Decimal(0)
        return Recommendation(
            recommendation_id=self.decision_id,
            match_id=self.match_id or UUID(int=0),
            status=self.status,
            created_at=self.decided_at,
            expires_at=self.expires_at,
            stake=self.stake,
            central_probability=self.central_probability or Decimal(0),
            conservative_probability=self.conservative_probability or Decimal(0),
            expected_value=Money(amount=ev.quantize(CENT, rounding=ROUND_FLOOR)),
            gates=tuple(outcome.to_result() for outcome in self.gates),
            lineage=self.lineage,
        )


def _gate(gate: Gate, failures: list[str], code: ReasonCode) -> GateOutcome:
    if failures:
        return GateOutcome(gate=gate, passed=False, code=code, detail=tuple(failures))
    return GateOutcome(gate=gate, passed=True)


def _policy_problems(policy: DecisionPolicy | None, at: datetime) -> list[str]:
    if policy is None:
        return ["DECISION_POLICY_MISSING"]
    problems = []
    if policy.state != "APPROVED":
        problems.append("DECISION_POLICY_NOT_APPROVED")
    if not policy.contains(at):
        problems.append("DECISION_POLICY_OUTSIDE_EFFECTIVE_INTERVAL")
    if policy.reviewed_at is None or policy.reviewed_at > at:
        problems.append("DECISION_POLICY_NOT_REVIEWED")
    elif policy.review_due_at is None or at >= policy.review_due_at:
        problems.append("DECISION_POLICY_REVIEW_EXPIRED")
    return problems


def _settled_probabilities(
    model: ModelAssessment, policy: DecisionPolicy | None
) -> tuple[Decimal, Decimal] | None:
    """Probabilities of a settled win. Sporting-win output needs an approved void stress."""
    if model.semantics == "SETTLED_WIN":
        return model.probability, model.conservative_probability
    if policy is None or policy.void_stress_probability is None:
        return None
    keep = 1 - policy.void_stress_probability
    # Conservative: a stressed void is treated as a loss, not as a returned stake.
    return model.probability * keep, model.conservative_probability * keep


def _value_at(
    payout: Callable[[Money], PayoutResolution],
    stake: Decimal,
    probabilities: tuple[Decimal, Decimal],
) -> tuple[BinaryValue | None, PayoutResolution]:
    resolution = payout(Money(amount=stake))
    cash = resolution.cash_return_if_win
    if not resolution.actionable or cash is None:
        return None, resolution
    value = binary_value(
        probability=probabilities[0],
        conservative_probability=probabilities[1],
        stake=stake,
        cash_return=cash.amount,
    )
    return value, resolution


def decide(inputs: DecisionInputs) -> DecisionRecord:
    """Run every gate in order and map the outcome to BET, WATCH or NO_BET."""
    at = require_aware(inputs.decided_at)
    policy = inputs.decision_policy
    quote = inputs.quote
    resolution = inputs.event_resolution
    model = inputs.model
    gates: list[GateOutcome] = []

    gates.append(
        _gate(Gate.DECISION_POLICY, _policy_problems(policy, at), ReasonCode.REVIEW_REQUIRED)
    )

    identity: list[str] = []
    if quote is None:
        identity.append("QUOTE_MISSING")
    if resolution is None:
        identity.append("EVENT_RESOLUTION_MISSING")
    elif resolution.blocks_recommendations:
        identity.extend(["EVENT_UNRESOLVED", *resolution.reasons])
    elif quote is not None and resolution.match_id != quote.match_id:
        identity.append("QUOTE_MATCH_MISMATCH")
    gates.append(_gate(Gate.IDENTITY, identity, ReasonCode.REVIEW_REQUIRED))

    actionability = inputs.actionability
    started: list[str] = []
    start_reasons = {
        ActionabilityReason.EVENT_STARTED,
        ActionabilityReason.EVENT_CANCELLED,
        ActionabilityReason.START_UNKNOWN,
        ActionabilityReason.EVENT_STATE_UNKNOWN,
    }
    if actionability is None or quote is None:
        started.append("START_STATE_UNKNOWN")
    else:
        started.extend(reason.value for reason in actionability.reasons if reason in start_reasons)
        if at >= quote.scheduled_start:
            started.append("SCHEDULED_START_PASSED")
    gates.append(_gate(Gate.NOT_STARTED, sorted(set(started)), ReasonCode.UNSUPPORTED_SCOPE))

    fresh: list[str] = []
    if actionability is None:
        fresh.append("ACTIONABILITY_MISSING")
    else:
        if actionability.evaluated_at > at:
            fresh.append("ACTIONABILITY_FROM_FUTURE")
        if not actionability.actionable:
            fresh.extend(reason.value for reason in actionability.reasons)
        elif actionability.expires_at is None or at >= actionability.expires_at:
            fresh.append("QUOTE_EXPIRED")
    gates.append(_gate(Gate.QUOTE_FRESH, fresh, ReasonCode.TRANSIENT_FAILURE))

    market: list[str] = []
    if quote is None:
        market.append("QUOTE_MISSING")
    else:
        if quote.market != Market.MATCH_WINNER:
            market.append("UNSUPPORTED_MARKET")
        participants = (
            {item.player_id for item in resolution.participants}
            if resolution is not None
            else set()
        )
        if quote.selection_player_id not in participants:
            market.append("SELECTION_NOT_A_PARTICIPANT")
    gates.append(_gate(Gate.MARKET, market, ReasonCode.UNSUPPORTED_SCOPE))

    rules = inputs.stake_rules
    rule_failures: list[str] = []
    if not inputs.settlement_rule.ok:
        rule_failures.append(f"SETTLEMENT_RULE:{inputs.settlement_rule.reason}")
    if not inputs.publication.allowed:
        rule_failures.append(f"PUBLICATION:{inputs.publication.reason}")
    minimum_payout: PayoutResolution | None = None
    if rules is None:
        rule_failures.append("STAKE_RULES_MISSING")
    else:
        minimum_payout = inputs.payout(Money(amount=rules.minimum))
        if not minimum_payout.actionable:
            rule_failures.extend(f"PAYOUT:{reason}" for reason in minimum_payout.reasons)
    gates.append(_gate(Gate.RULES, rule_failures, ReasonCode.MISSING_EVIDENCE))

    quality = inputs.quality
    quality_failures = (
        ["QUALITY_MISSING"] if quality is None else [f"QUALITY:{c}" for c in quality.hard_failures]
    )
    if quality is not None and not quality.usable and not quality.hard_failures:
        quality_failures.append("QUALITY_NOT_USABLE")
    gates.append(_gate(Gate.QUALITY, quality_failures, ReasonCode.MISSING_EVIDENCE))

    probabilities = _settled_probabilities(model, policy) if model is not None else None
    domain: list[str] = []
    if model is None:
        domain.append("MODEL_MISSING")
    else:
        if model.in_supported_domain is not True:
            domain.append(
                "OUT_OF_DOMAIN" if model.in_supported_domain is False else "DOMAIN_UNKNOWN"
            )
        if model.generated_at > at:
            domain.append("MODEL_OUTPUT_FROM_FUTURE")
        if probabilities is None:
            domain.append("CONDITIONAL_PROBABILITY_UNSUPPORTED")
    gates.append(_gate(Gate.MODEL_DOMAIN, domain, ReasonCode.UNSUPPORTED_SCOPE))

    calibrated = model is not None and model.calibrated is True
    gates.append(
        _gate(
            Gate.MODEL_CALIBRATED,
            [] if calibrated else ["CALIBRATION_NOT_DEMONSTRATED"],
            ReasonCode.REVIEW_REQUIRED,
        )
    )

    disagreement: list[str] = []
    if model is None or model.disagreement is None:
        disagreement.append("DISAGREEMENT_UNKNOWN")
    elif policy is None or model.disagreement > policy.max_model_disagreement:
        disagreement.append("DISAGREEMENT_ABOVE_LIMIT")
    gates.append(_gate(Gate.MODEL_DISAGREEMENT, disagreement, ReasonCode.REVIEW_REQUIRED))

    # Value at the minimum permitted stake decides between WATCH and NO_BET.
    value: BinaryValue | None = None
    if probabilities is not None and rules is not None:
        value, _ = _value_at(inputs.payout, rules.minimum, probabilities)
    minimum_roi = policy.minimum_conservative_roi if policy is not None else None
    gates.extend(_value_gates(value, minimum_roi))

    outlier: list[str] = []
    if value is None or model is None or policy is None:
        outlier.append("OUTLIER_CHECK_UNAVAILABLE")
    elif value.probability_edge > policy.max_unconfirmed_edge:
        consensus = inputs.consensus_probability
        if consensus is None:
            outlier.append("EDGE_NEEDS_CONSENSUS")
        elif abs(consensus - model.probability) > policy.consensus_tolerance:
            outlier.append("CONSENSUS_DISAGREES")
    gates.append(_gate(Gate.OUTLIER, outlier, ReasonCode.REVIEW_REQUIRED))

    room: Capacity | None = None
    risk: list[str] = []
    responsible = inputs.responsible_use
    if policy is None or rules is None or inputs.exposure is None or responsible.policy is None:
        risk.append("RISK_STATE_UNAVAILABLE")
    else:
        room = capacity(policy, responsible.policy, inputs.exposure, rules)
        risk.extend(reason.value for reason in room.reasons)
    gates.append(_gate(Gate.RISK_BUDGET, risk, ReasonCode.LIMIT_REACHED))

    responsible_failures = (
        []
        if responsible.decision.allowed and responsible.policy is not None
        else [f"RESPONSIBLE_USE:{responsible.decision.reason}"]
    )
    gates.append(_gate(Gate.RESPONSIBLE_USE, responsible_failures, ReasonCode.LIMIT_REACHED))

    hard_failed = [item.gate for item in gates if not item.passed and item.gate not in VALUE_GATES]
    value_failed = [item.gate for item in gates if not item.passed and item.gate in VALUE_GATES]
    stake = Decimal("0.00")
    final_value = value
    payout_source = (
        minimum_payout.source.value if minimum_payout and minimum_payout.source else None
    )
    if not hard_failed and not value_failed:
        assert policy is not None and rules is not None and room is not None
        assert probabilities is not None and inputs.exposure is not None
        sizing = size_stake(
            conservative_probability=probabilities[1],
            bankroll=inputs.exposure.bankroll,
            maximum_stake=room.maximum_stake,
            rules=rules,
            kelly_fraction=policy.kelly_fraction,
            payout=lambda amount: _cash(inputs.payout, amount),
            max_steps=policy.max_search_steps,
        )
        if sizing.stake is None:
            gates.append(_gate(Gate.STAKE, [str(sizing.reason)], ReasonCode.VALUE_INSUFFICIENT))
        else:
            # Recheck every value gate at the final rounded stake and its own payout.
            final_value, final_payout = _value_at(inputs.payout, sizing.stake, probabilities)
            final_gates = _value_gates(final_value, policy.minimum_conservative_roi)
            replaced = {item.gate: item for item in final_gates}
            gates = [replaced.get(item.gate, item) for item in gates]
            failed_final = [item for item in final_gates if not item.passed]
            if failed_final or final_value is None:
                gates.append(
                    _gate(
                        Gate.STAKE, ["VALUE_FAILED_AT_FINAL_STAKE"], ReasonCode.VALUE_INSUFFICIENT
                    )
                )
            else:
                gates.append(GateOutcome(gate=Gate.STAKE, passed=True))
                stake = sizing.stake
                payout_source = final_payout.source.value if final_payout.source else None
    else:
        gates.append(
            _gate(Gate.STAKE, ["NOT_SIZED_AFTER_FAILED_GATES"], ReasonCode.VALUE_INSUFFICIENT)
        )

    failed = tuple(item.gate for item in gates if not item.passed)
    hard = [gate for gate in failed if gate not in VALUE_GATES and gate != Gate.STAKE]
    central_positive = final_value is not None and final_value.expected_value > 0
    conservative_failed = any(gate in VALUE_GATES for gate in failed)
    if not failed:
        status = RecommendationStatus.BET
    elif not hard and central_positive and conservative_failed:
        # A positive central edge whose conservative criteria fail. A stake below the
        # bookmaker minimum with value intact is an abstention (NO_BET), not WATCH.
        status = RecommendationStatus.WATCH
    else:
        status = RecommendationStatus.NO_BET
    if status != RecommendationStatus.BET:
        stake = Decimal("0.00")

    ttl = policy.no_bet_ttl_seconds if policy is not None else 60
    expires_at = at + timedelta(seconds=ttl)
    if status == RecommendationStatus.BET:
        assert actionability is not None and actionability.expires_at is not None
        assert policy is not None
        expires_at = min(
            actionability.expires_at, at + timedelta(seconds=policy.reservation_ttl_seconds)
        )

    versions = [inputs.settlement_rule.rule.version] if inputs.settlement_rule.rule else []
    if policy is not None:
        versions.append(policy.version)
    if responsible.policy is not None:
        versions.append(responsible.policy.version)
    if minimum_payout is not None:
        versions.extend(minimum_payout.rule_versions)
        if minimum_payout.policy_version:
            versions.append(minimum_payout.policy_version)
    if actionability is not None:
        versions.append(actionability.policy_version)

    summary = None
    if final_value is not None:
        summary = ValueSummary(
            stake=Money(amount=final_value.stake),
            cash_return_if_win=Money(amount=final_value.cash_return_if_win),
            break_even_probability=final_value.break_even_probability,
            expected_value=final_value.expected_value,
            expected_roi=final_value.expected_roi,
            conservative_expected_value=final_value.conservative_expected_value,
            conservative_roi=final_value.conservative_roi,
            probability_edge=final_value.probability_edge,
        )
    return DecisionRecord(
        decision_id=stable_id("decision", f"{inputs.ledger_id}:{inputs.decision_key}"),
        decision_key=inputs.decision_key,
        status=status,
        decided_at=at,
        expires_at=expires_at,
        ledger_id=inputs.ledger_id,
        match_id=quote.match_id if quote else None,
        selection_player_id=quote.selection_player_id if quote else None,
        bookmaker=quote.bookmaker if quote else None,
        quote_id=quote.quote_id if quote else None,
        decimal_odds=quote.decimal_odds if quote else None,
        stake=Money(amount=stake),
        central_probability=probabilities[0] if probabilities else None,
        conservative_probability=probabilities[1] if probabilities else None,
        probability_semantics=model.semantics if model else None,
        value=summary,
        capacity=room,
        gates=tuple(gates),
        failed_gates=failed,
        payout_source=payout_source,
        policy_versions=tuple(dict.fromkeys(versions)),
        lineage=inputs.lineage,
    )


def _cash(payout: Callable[[Money], PayoutResolution], amount: Decimal) -> Decimal | None:
    resolution = payout(Money(amount=amount))
    cash = resolution.cash_return_if_win
    return cash.amount if resolution.actionable and cash is not None else None


def _value_gates(value: BinaryValue | None, minimum_roi: Decimal | None) -> list[GateOutcome]:
    if value is None:
        missing = ["VALUE_UNAVAILABLE"]
        return [
            _gate(gate, missing, ReasonCode.VALUE_INSUFFICIENT)
            for gate in (Gate.CENTRAL_EV, Gate.CONSERVATIVE_EV, Gate.CONSERVATIVE_ROI)
        ]
    roi_failures = []
    if minimum_roi is None:
        roi_failures.append("MINIMUM_ROI_UNKNOWN")
    elif value.conservative_roi < minimum_roi:
        roi_failures.append("CONSERVATIVE_ROI_BELOW_MINIMUM")
    return [
        _gate(
            Gate.CENTRAL_EV,
            [] if value.expected_value > 0 else ["CENTRAL_EV_NOT_POSITIVE"],
            ReasonCode.VALUE_INSUFFICIENT,
        ),
        _gate(
            Gate.CONSERVATIVE_EV,
            [] if value.conservative_expected_value > 0 else ["CONSERVATIVE_EV_NOT_POSITIVE"],
            ReasonCode.VALUE_INSUFFICIENT,
        ),
        _gate(Gate.CONSERVATIVE_ROI, roi_failures, ReasonCode.VALUE_INSUFFICIENT),
    ]


class PublicationOutcome(Contract):
    published: bool
    record: DecisionRecord
    reservation_id: UUID | None = None
    reasons: tuple[str, ...] = ()


def _superseding_no_bet(
    record: DecisionRecord, gate: Gate, detail: list[str], at: datetime
) -> DecisionRecord:
    """A new NO_BET version; the original record is kept unchanged for audit."""
    gates = []
    found = False
    for item in record.gates:
        if item.gate == gate:
            found = True
            item = GateOutcome(
                gate=item.gate,
                passed=False,
                code=item.code or ReasonCode.TRANSIENT_FAILURE,
                detail=(*item.detail, *detail),
            )
        gates.append(item)
    if not found:
        gates.append(
            GateOutcome(
                gate=gate, passed=False, code=ReasonCode.TRANSIENT_FAILURE, detail=tuple(detail)
            )
        )
    data = record.model_dump() | {
        "decision_id": stable_id("decision", f"{record.decision_id}:v{record.version + 1}"),
        "version": record.version + 1,
        "supersedes": record.decision_id,
        "status": RecommendationStatus.NO_BET,
        "decided_at": at,
        "expires_at": max(record.expires_at, at + timedelta(seconds=1)),
        "stake": Money(amount=Decimal("0.00")),
        "gates": tuple(gates),
        "failed_gates": tuple(item.gate for item in gates if not item.passed),
    }
    return DecisionRecord.model_validate(data)


def prepare_publication(
    record: DecisionRecord,
    *,
    now: datetime,
    publication: Decision,
    actionability: Actionability | None,
    responsible_use: PolicyLookup[ResponsibleUsePolicy],
    policy: DecisionPolicy,
    rules: StakeRules,
    reserve: Callable[[Decimal, Callable[[ExposureState], Decimal]], UUID],
) -> PublicationOutcome:
    """Recheck volatile gates at publication time and reserve a BET stake.

    `reserve(stake, allowed_stake)` must run `allowed_stake` on the exposure state that it
    reads under its lock (for example through `ExposureService.reserve`) and raise a
    `ValueError` when the stake does not fit. A cached decision never outlives its expiry
    or a kill switch.
    """
    now = require_aware(now)
    if now >= record.expires_at:
        return PublicationOutcome(published=False, record=record, reasons=("DECISION_EXPIRED",))
    if not publication.allowed:
        detail = f"PUBLICATION:{publication.reason}"
        if record.status != RecommendationStatus.BET:
            return PublicationOutcome(published=False, record=record, reasons=(detail,))
        return PublicationOutcome(
            published=False,
            record=_superseding_no_bet(record, Gate.RULES, [detail], now),
            reasons=(detail,),
        )
    if record.status != RecommendationStatus.BET:
        return PublicationOutcome(published=True, record=record)

    problems: list[tuple[Gate, str]] = []
    if actionability is None or not actionability.actionable or actionability.evaluated_at > now:
        reasons = actionability.reasons if actionability is not None else ()
        codes = ",".join(reason.value for reason in reasons) or "UNAVAILABLE"
        problems.append((Gate.QUOTE_FRESH, f"RECHECK:{codes}"))
    elif actionability.expires_at is None or now >= actionability.expires_at:
        problems.append((Gate.QUOTE_FRESH, "RECHECK:QUOTE_EXPIRED"))
    elif (
        actionability.observation is None
        or actionability.observation.quote.decimal_odds != record.decimal_odds
    ):
        problems.append((Gate.QUOTE_FRESH, "RECHECK:PRICE_CHANGED"))
    if not responsible_use.decision.allowed or responsible_use.policy is None:
        problems.append((Gate.RESPONSIBLE_USE, f"RECHECK:{responsible_use.decision.reason}"))
    if problems:
        superseded = record
        for gate, detail in problems:
            superseded = _superseding_no_bet(superseded, gate, [detail], now)
        return PublicationOutcome(
            published=False, record=superseded, reasons=tuple(detail for _, detail in problems)
        )

    responsible = responsible_use.policy
    assert responsible is not None

    def allowed_stake(state: ExposureState) -> Decimal:
        room = capacity(policy, responsible, state, rules)
        return room.maximum_stake if room.available else Decimal(0)

    try:
        reservation_id = reserve(record.stake.amount, allowed_stake)
    except ValueError as error:
        detail = f"RESERVATION:{type(error).__name__}"
        return PublicationOutcome(
            published=False,
            record=_superseding_no_bet(record, Gate.RISK_BUDGET, [detail], now),
            reasons=(detail,),
        )
    return PublicationOutcome(published=True, record=record, reservation_id=reservation_id)
