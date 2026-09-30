"""F13.2 to F13.5 execution replay: the production F05/F06/F12 code on recorded history.

Each case is one decision at one cutoff. The replay rebuilds the state known at the cutoff
from append-only history: the event mapping, the quote and its actionability, the rules
and policies in effect, and the exposure of the replay ledger. It then runs F12
``decide`` unchanged.

A BET is then executed at ``cutoff + latency``. The replay takes the first quote observed
at or after that time and runs ``decide`` again with the same policy, so a price, risk or
capacity change cancels or resizes the bet. This later observation evaluates execution
only; the prediction and its features stay those of the cutoff. ``prepare_publication``
rechecks the volatile gates and reserves the stake. F06 settles each struck bet against
every result version, and a corrected result becomes a ledger correction.

A result is execution-grade only when nothing was assumed: observed or verified-archive
quotes, no research-only case, an observed accepted-stake cap for every struck bet, and a
scenario without stress settings. Otherwise every assumption is named.
"""

import hashlib
import heapq
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_FLOOR, Decimal
from enum import StrEnum
from functools import partial
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from tennis_engine.common.clock import FrozenClock, require_aware
from tennis_engine.common.contracts import Amount, Contract, Identifier, Money, Timestamp
from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import AuditLineage, Market, RecommendationStatus
from tennis_engine.features.quality import QualityReport
from tennis_engine.governance.contracts import Decision, ResponsibleUsePolicy
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.ingestion.bookmakers.contracts import CanonicalQuote, QuoteState
from tennis_engine.ingestion.bookmakers.history import QuoteHistory, QuoteKey
from tennis_engine.ingestion.bookmakers.quotes import (
    Actionability,
    ActionabilityPolicy,
    QuoteObservation,
)
from tennis_engine.normalization.contracts import MatchResolution
from tennis_engine.pricing.decision import (
    DecisionInputs,
    DecisionRecord,
    ModelAssessment,
    decide,
    prepare_publication,
)
from tennis_engine.pricing.payout import PayoutBreakdown, PayoutResolution
from tennis_engine.pricing.reservation import (
    ExposureService,
    MemoryReservationStore,
    ReservationRequest,
)
from tennis_engine.pricing.risk import DecisionPolicy, ExposureState, StakeRules
from tennis_engine.settlement.engine import (
    MatchOutcome,
    SettlementContext,
    SettlementResult,
    SettlementStatus,
    settle,
)
from tennis_engine.settlement.ledger import (
    InMemoryLedgerStore,
    LedgerReconciliation,
    VirtualBet,
    VirtualLedgerService,
)
from tennis_engine.settlement.rules import RuleLookup, RuleRegistry, SettlementRule

from .economics import EconomicSummary, ReplayBet, summarize

CENT = Decimal("0.01")
MINIMUM_ODDS = Decimal("1.01")
END_OF_TIME = datetime.max.replace(tzinfo=UTC)
CLOSING_POLICY = "same-quote-key-last-open-before-start-v1"


class AcceptedStakeCap(Contract):
    """An observed maximum accepted stake. Zero is an observed rejection."""

    bookmaker: Identifier
    match_id: UUID | None  # None: the cap applies to every match of the bookmaker.
    maximum_stake: Amount
    observed_at: Timestamp
    evidence_id: Identifier


class ExecutionScenario(Contract):
    """Execution settings. Every stress setting is an assumption, never evidence."""

    name: Identifier
    latency_seconds: Annotated[int, Field(ge=0, strict=True)] = 0
    # Executed odds are 1 + (odds - 1) * (1 - odds_haircut), rounded down to 0.01.
    odds_haircut: Annotated[Decimal, Field(ge=0, lt=1)] = Decimal(0)
    # Deterministic share of executions treated as a suspended market.
    suspension_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal(0)
    # Multiplies the observed or assumed accepted-stake cap.
    capacity_fraction: Annotated[Decimal, Field(gt=0, le=1)] = Decimal(1)
    # Cap used when no cap is observed. None accepts the full stake, as an assumption.
    assumed_cap: Amount | None = None
    seed: int = 0

    def stress_assumptions(self) -> tuple[str, ...]:
        labels = []
        if self.odds_haircut:
            labels.append(f"odds haircut {self.odds_haircut}")
        if self.suspension_rate:
            labels.append(f"suspension rate {self.suspension_rate}")
        if self.capacity_fraction != 1:
            labels.append(f"capacity fraction {self.capacity_fraction}")
        return tuple(labels)


class ReplayCase(Contract):
    """One decision at one cutoff. The model output must be known by the cutoff."""

    case_id: Identifier
    block: Identifier  # Tournament week, for the block bootstrap.
    decided_at: Timestamp
    quote_key: tuple[str, str, str, str]
    model: ModelAssessment | None
    consensus_probability: Decimal | None
    quality: QualityReport | None
    lineage: AuditLineage
    research_only: bool = False


QuoteOrigin = Literal["PROSPECTIVE", "ARCHIVED_VERIFIED", "RECONSTRUCTED"]


@dataclass(frozen=True)
class ReplayServices:
    """Production lookups. Each one takes the time and returns what was in effect then."""

    history: QuoteHistory
    actionability_policy: ActionabilityPolicy
    quote_origin: QuoteOrigin
    settlement_rule: Callable[[str, datetime], RuleLookup[SettlementRule]]
    payout: Callable[[CanonicalQuote, datetime], Callable[[Money], PayoutResolution]]
    stake_rules: Callable[[str], StakeRules | None]
    publication: Callable[[datetime], Decision]
    responsible_use: Callable[[datetime], PolicyLookup[ResponsibleUsePolicy]]
    decision_policy: Callable[[datetime], DecisionPolicy | None]
    registry: RuleRegistry
    outcomes: Callable[[UUID], Sequence[MatchOutcome]]
    stake_caps: Sequence[AcceptedStakeCap] = field(default_factory=tuple)


class ExecutionStatus(StrEnum):
    NOT_BET = "NOT_BET"  # The cutoff decision was WATCH or NO_BET.
    CANCELLED = "CANCELLED"  # A BET that execution cancelled.
    STRUCK = "STRUCK"


class CaseOutcome(Contract):
    case_id: Identifier
    decision: DecisionRecord
    actionable_at_cutoff: bool
    status: ExecutionStatus
    reasons: tuple[str, ...] = ()
    executed_at: Timestamp | None = None
    execution: DecisionRecord | None = None
    bet: VirtualBet | None = None
    cap_evidence_id: Identifier | None = None
    assumptions: tuple[str, ...] = ()


class ReplayRun(Contract):
    run_id: Identifier
    scenario: ExecutionScenario
    quote_origin: QuoteOrigin
    cases: tuple[CaseOutcome, ...]
    settlements: tuple[SettlementResult, ...]
    bets: tuple[ReplayBet, ...]
    execution_grade: bool
    assumptions: tuple[str, ...]
    reconciliation: LedgerReconciliation

    def summary(
        self, *, starting_bankroll: Decimal, draws: int, seed: int, level: Decimal
    ) -> EconomicSummary:
        return summarize(
            self.bets,
            decisions=[case.decision.status.value for case in self.cases],
            actionable_decisions=sum(case.actionable_at_cutoff for case in self.cases),
            starting_bankroll=starting_bankroll,
            execution_grade=self.execution_grade,
            assumptions=self.assumptions,
            draws=draws,
            seed=seed,
            level=level,
        )


def canonical_quote(
    observation: QuoteObservation, resolution: MatchResolution | None
) -> CanonicalQuote | None:
    """The F05 mapping of one observation, or None when it cannot be mapped."""
    raw = observation.quote
    if (
        resolution is None
        or resolution.blocks_recommendations
        or resolution.match_id is None
        or raw.market != Market.MATCH_WINNER
        or raw.participant_index is None
        or observation.scheduled_start is None
    ):
        return None
    player_id = resolution.participants[raw.participant_index].player_id
    if player_id is None:
        return None
    return CanonicalQuote(
        quote_id=stable_id(
            "bookmaker-quote",
            ":".join(
                (*raw.key, observation.observed_at.isoformat(), observation.raw_content_sha256)
            ),
        ),
        bookmaker=raw.bookmaker,
        match_id=resolution.match_id,
        market=raw.market,
        selection_player_id=player_id,
        decimal_odds=raw.decimal_odds,
        state=raw.state,
        source_event_id=raw.source_event_id,
        source_market_id=raw.source_market_id,
        source_selection_id=raw.source_selection_id,
        source_order_swapped=bool(resolution.swapped),
        scheduled_start=observation.scheduled_start,
        promotion_marker=raw.promotion_marker,
        observed_at=observation.observed_at,
        parser_version=observation.parser_version,
        raw_content_sha256=observation.raw_content_sha256,
        resolution_policy_version=resolution.policy_version,
    )


def _haircut(odds: Decimal, haircut: Decimal) -> Decimal:
    if not haircut:
        return odds
    reduced = (1 + (odds - 1) * (1 - haircut)).quantize(CENT, rounding=ROUND_DOWN)
    return max(MINIMUM_ODDS, reduced)


def _fraction(seed: int, case_id: str) -> Decimal:
    digest = hashlib.sha256(f"{seed}:{case_id}".encode()).hexdigest()[:15]
    return Decimal(int(digest, 16)) / Decimal(16**15)


@dataclass
class _State:
    resolution: MatchResolution | None
    actionability: Actionability
    quote: CanonicalQuote | None


@dataclass(order=True)
class _Event:
    at: datetime
    priority: int  # Settlements first, then executions, then decisions.
    sequence: int
    action: Callable[[], None] = field(compare=False)


class _Replay:
    def __init__(
        self,
        services: ReplayServices,
        scenario: ExecutionScenario,
        run_id: str,
        opening_balance: Money,
        start: datetime,
    ) -> None:
        self.services = services
        self.scenario = scenario
        self.ledger_id = run_id
        self.clock = FrozenClock(start)
        self.ledger = VirtualLedgerService(InMemoryLedgerStore(), self.clock)
        self.exposure = ExposureService(MemoryReservationStore(), self.ledger, self.clock)
        self.ledger.open_ledger(run_id, opening_balance)
        self.events: list[_Event] = []
        self.sequence = 0
        self.outcomes: dict[str, CaseOutcome] = {}
        self.settlements: list[SettlementResult] = []
        self.final: dict[UUID, SettlementResult] = {}
        self.struck: dict[UUID, tuple[VirtualBet, str, QuoteKey]] = {}

    def schedule(self, at: datetime, priority: int, action: Callable[[], None]) -> None:
        self.sequence += 1
        heapq.heappush(self.events, _Event(at, priority, self.sequence, action))

    def run(self) -> None:
        while self.events:
            event = heapq.heappop(self.events)
            self.clock.instant = max(self.clock.instant, event.at)
            event.action()

    # State at a time (F13.2).

    def state(self, key: QuoteKey, at: datetime, haircut: Decimal = Decimal(0)) -> _State:
        history = self.services.history
        mappings = history.store.mappings(key[0], key[1], at)
        resolution = mappings[-1] if mappings else None
        actionability = history.actionability(key, at=at, policy=self.services.actionability_policy)
        observation = actionability.observation
        if observation is not None and haircut:
            raw = observation.quote
            observation = observation.model_copy(
                update={
                    "quote": raw.model_copy(
                        update={"decimal_odds": _haircut(raw.decimal_odds, haircut)}
                    )
                }
            )
            actionability = actionability.model_copy(update={"observation": observation})
        quote = canonical_quote(observation, resolution) if observation is not None else None
        return _State(resolution, actionability, quote)

    def inputs(
        self,
        case: ReplayCase,
        key: str,
        at: datetime,
        state: _State,
        rules: StakeRules | None,
    ) -> DecisionInputs:
        quote = state.quote
        bookmaker = case.quote_key[0]
        exposure = (
            self.exposure.exposure(
                self.ledger_id, match_id=quote.match_id, bookmaker=bookmaker, at=at
            )
            if quote is not None
            else None
        )
        payout = self.services.payout(quote, at) if quote is not None else _no_payout
        return DecisionInputs(
            decision_key=key,
            decided_at=at,
            ledger_id=self.ledger_id,
            quote=quote,
            event_resolution=state.resolution,
            actionability=state.actionability,
            settlement_rule=self.services.settlement_rule(bookmaker, at),
            payout=payout,
            stake_rules=rules,
            quality=case.quality,
            model=case.model,
            consensus_probability=case.consensus_probability,
            publication=self.services.publication(at),
            responsible_use=self.services.responsible_use(at),
            decision_policy=self.services.decision_policy(at),
            exposure=exposure,
            lineage=case.lineage,
        )

    def decide_case(self, case: ReplayCase) -> None:
        at = case.decided_at
        state = self.state(case.quote_key, at)
        rules = self.services.stake_rules(case.quote_key[0])
        record = decide(self.inputs(case, case.case_id, at, state, rules))
        outcome = CaseOutcome(
            case_id=case.case_id,
            decision=record,
            actionable_at_cutoff=state.actionability.actionable,
            status=ExecutionStatus.NOT_BET,
        )
        self.outcomes[case.case_id] = outcome
        if record.status != RecommendationStatus.BET:
            return
        action_at = at + timedelta(seconds=self.scenario.latency_seconds)
        # The first observation at or after the action time. It is later than the cutoff
        # on purpose: it evaluates execution, not the prediction.
        later = [
            item
            for item in self.services.history.store.observations(case.quote_key, END_OF_TIME)
            if item.observed_at >= action_at
        ]
        start = state.quote.scheduled_start if state.quote is not None else None
        if not later or (start is not None and later[0].observed_at >= start):
            self.outcomes[case.case_id] = outcome.model_copy(
                update={
                    "status": ExecutionStatus.CANCELLED,
                    "reasons": ("NO_QUOTE_AT_ACTION_TIME",),
                }
            )
            return
        executed_at = later[0].observed_at
        self.schedule(executed_at, 1, partial(self.execute, case, executed_at))

    # Execution (F13.3, F13.4).

    def cap(self, bookmaker: str, match_id: UUID, at: datetime) -> AcceptedStakeCap | None:
        known = [
            item
            for item in self.services.stake_caps
            if item.bookmaker == bookmaker
            and item.match_id in (None, match_id)
            and item.observed_at <= at
        ]
        # The latest observation wins; a match-specific cap wins a tie.
        known.sort(key=lambda item: (item.observed_at, item.match_id is not None))
        return known[-1] if known else None

    def execute(self, case: ReplayCase, at: datetime) -> None:
        outcome = self.outcomes[case.case_id]
        scenario = self.scenario

        def cancel(*reasons: str, **update: object) -> None:
            self.outcomes[case.case_id] = outcome.model_copy(
                update={
                    "status": ExecutionStatus.CANCELLED,
                    "reasons": reasons,
                    "executed_at": at,
                }
                | update
            )

        if scenario.suspension_rate and _fraction(scenario.seed, case.case_id) < (
            scenario.suspension_rate
        ):
            cancel("SCENARIO_SUSPENDED", assumptions=("suspension scenario",))
            return
        state = self.state(case.quote_key, at, scenario.odds_haircut)
        base = self.services.stake_rules(case.quote_key[0])
        assumptions: list[str] = []
        observed: AcceptedStakeCap | None = None
        rules = base
        if state.quote is not None and base is not None:
            observed = self.cap(case.quote_key[0], state.quote.match_id, at)
            if observed is not None:
                limit: Decimal | None = observed.maximum_stake
            elif scenario.assumed_cap is not None:
                limit = scenario.assumed_cap
                assumptions.append("assumed accepted-stake cap")
            else:
                limit = None
                assumptions.append("no observed accepted-stake cap; full stake accepted")
            if limit is not None:
                limit = (limit * scenario.capacity_fraction).quantize(CENT, rounding=ROUND_FLOOR)
                maximum = limit if base.maximum is None else min(base.maximum, limit)
                rules = base.model_copy(update={"maximum": maximum})
        record = decide(self.inputs(case, f"{case.case_id}:execution", at, state, rules))
        cap_id = observed.evidence_id if observed is not None else None
        common = {"execution": record, "cap_evidence_id": cap_id}
        if record.status != RecommendationStatus.BET:
            cancel(
                *(f"EXECUTION:{gate.value}" for gate in record.failed_gates),
                assumptions=tuple(assumptions),
                **common,
            )
            return
        policy = self.services.decision_policy(at)
        assert policy is not None and rules is not None and state.quote is not None
        quote = state.quote

        def reserve(stake: Decimal, allowed: Callable[[ExposureState], Decimal]) -> UUID:
            request = ReservationRequest(
                decision_key=record.decision_key,
                ledger_id=self.ledger_id,
                match_id=quote.match_id,
                bookmaker=quote.bookmaker,
                selection_player_id=quote.selection_player_id,
                stake=Money(amount=stake),
                ttl_seconds=policy.reservation_ttl_seconds,
            )
            return self.exposure.reserve(request, allowed).reservation_id

        published = prepare_publication(
            record,
            now=at,
            publication=self.services.publication(at),
            actionability=state.actionability,
            responsible_use=self.services.responsible_use(at),
            policy=policy,
            rules=rules,
            reserve=reserve,
        )
        if not published.published or published.reservation_id is None:
            cancel(
                *published.reasons,
                assumptions=tuple(assumptions),
                execution=published.record,
                cap_evidence_id=cap_id,
            )
            return
        assert record.value is not None and record.decimal_odds is not None
        rule = self.services.settlement_rule(quote.bookmaker, at).rule
        payout = self.services.payout(quote, at)(record.stake)
        assert rule is not None and payout.policy_version is not None
        bet = VirtualBet(
            bet_id=published.reservation_id,
            ledger_id=self.ledger_id,
            decision_id=record.decision_id,
            bookmaker=quote.bookmaker,
            match_id=quote.match_id,
            selection_player_id=quote.selection_player_id,
            decimal_odds=record.decimal_odds,
            stake=record.stake,
            cash_return_if_win=record.value.cash_return_if_win,
            payout_policy_version=payout.policy_version,
            settlement_rule_version=rule.version,
            struck_at=at,
        )
        self.exposure.commit(published.reservation_id, bet)
        self.struck[bet.bet_id] = (bet, case.block, case.quote_key)
        self.outcomes[case.case_id] = outcome.model_copy(
            update={
                "status": ExecutionStatus.STRUCK,
                "executed_at": at,
                "execution": record,
                "bet": bet,
                "cap_evidence_id": cap_id,
                "assumptions": tuple(assumptions),
            }
        )
        breakdown = payout.breakdown
        for version in self.services.outcomes(bet.match_id):
            if version.observed_at >= at:
                self.schedule(
                    version.observed_at,
                    0,
                    partial(self.settle, bet, version, breakdown),
                )

    # Settlement (F13.5).

    def settle(
        self, bet: VirtualBet, version: MatchOutcome, breakdown: PayoutBreakdown | None
    ) -> None:
        # Tax components are unknown when the payout came only from a coupon preview.
        stake_tax = breakdown.stake_tax if breakdown is not None else None
        winnings_tax = breakdown.winnings_tax if breakdown is not None else None
        result = settle(
            SettlementContext(
                bet_id=bet.bet_id,
                bookmaker=bet.bookmaker,
                rule_version=bet.settlement_rule_version,
                match_id=bet.match_id,
                selection_player_id=bet.selection_player_id,
                stake=bet.stake,
                displayed_odds=bet.decimal_odds,
                cash_return_if_win=bet.cash_return_if_win,
                stake_tax=stake_tax,
                winnings_tax_if_win=winnings_tax,
                bet_time=bet.struck_at,
                settled_at=version.observed_at,
                outcome=version,
            ),
            self.services.registry,
        )
        self.settlements.append(result)
        previous = self.final.get(bet.bet_id)
        if result.status == SettlementStatus.PENDING and previous is not None:
            return  # A final result is kept; a later disputed version stays on record.
        correction = None
        if previous is not None and previous.financial_digest() != result.financial_digest():
            correction = f"corrected result observed at {version.observed_at.isoformat()}"
        self.ledger.apply_settlement(self.ledger_id, result, correction_reason=correction)
        if result.final:
            self.final[bet.bet_id] = result

    def closing_odds(self, key: QuoteKey, start: datetime) -> Decimal | None:
        before = [
            item
            for item in self.services.history.store.observations(key, start)
            if item.observed_at < start
        ]
        if not before or before[-1].quote.state != QuoteState.OPEN:
            return None
        return before[-1].quote.decimal_odds


def _no_payout(stake: Money) -> PayoutResolution:
    return PayoutResolution(actionable=False, reasons=("QUOTE_MISSING",))


def replay(
    cases: Sequence[ReplayCase],
    services: ReplayServices,
    scenario: ExecutionScenario,
    *,
    run_id: str,
    opening_balance: Money,
) -> ReplayRun:
    """Replay cases in time order on a fresh virtual ledger. The run is deterministic."""
    if not cases:
        raise ValueError("A replay needs at least one case")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Case IDs must be unique")
    for case in cases:
        require_aware(case.decided_at)
        if case.model is not None and case.model.generated_at > case.decided_at:
            raise ValueError(f"Case {case.case_id} uses a model output from after its cutoff")
    start = min(case.decided_at for case in cases)
    state = _Replay(services, scenario, run_id, opening_balance, start)
    for case in sorted(cases, key=lambda item: (item.decided_at, item.case_id)):
        state.schedule(case.decided_at, 2, partial(state.decide_case, case))
    state.run()

    outcomes = tuple(state.outcomes[case.case_id] for case in cases)
    bets = []
    for bet_id, (bet, block, key) in sorted(state.struck.items(), key=lambda item: str(item[0])):
        final = state.final.get(bet_id)
        match = next(iter(services.outcomes(bet.match_id)), None)
        closing = state.closing_odds(key, match.scheduled_start) if match is not None else None
        bets.append(
            ReplayBet(
                bet_id=bet_id,
                block=block,
                decided_at=bet.struck_at,
                settled_at=final.settled_at if final is not None else None,
                stake=bet.stake.amount,
                cash_return=final.cash_return.amount
                if final is not None and final.cash_return is not None
                else None,
                odds=bet.decimal_odds,
                closing_odds=closing,
                closing_policy=CLOSING_POLICY if closing is not None else None,
            )
        )
    assumptions = list(scenario.stress_assumptions())
    if services.quote_origin == "RECONSTRUCTED":
        assumptions.append("quote history is reconstructed, not observed")
    research = sum(case.research_only for case in cases)
    if research:
        assumptions.append(f"{research} cases use research-only snapshots")
    counted: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.status == ExecutionStatus.STRUCK:
            for label in outcome.assumptions:
                counted[label] = counted.get(label, 0) + 1
    assumptions.extend(f"{count} bets: {label}" for label, count in sorted(counted.items()))
    return ReplayRun(
        run_id=run_id,
        scenario=scenario,
        quote_origin=services.quote_origin,
        cases=outcomes,
        settlements=tuple(state.settlements),
        bets=tuple(bets),
        execution_grade=not assumptions,
        assumptions=tuple(assumptions),
        reconciliation=state.ledger.reconcile(run_id),
    )
