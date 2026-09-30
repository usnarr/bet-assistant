"""F14.1 typed read contracts for stored decisions and their API views.

A stored decision is an F12 `DecisionRecord` plus its display context. The context holds
canonical facts only: names, schedule, evidence values and model components. It never
holds raw source payloads. API views are derived at read time and never change a record.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import (
    Contract,
    ExactDecimal,
    Identifier,
    Money,
    Probability,
    ReasonCode,
    Timestamp,
    VersionRef,
)
from tennis_engine.contracts.domain import Market, RecommendationStatus, Tour
from tennis_engine.pricing.decision import DecisionRecord

Text = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]


class Mode(StrEnum):
    """Only shadow operation exists. Limited release needs F16 approval first."""

    SHADOW = "SHADOW"


class FactKind(StrEnum):
    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    MISSING = "MISSING"


class PlayerRef(Contract):
    player_id: UUID
    display_name: Text


class MatchSummary(Contract):
    match_id: UUID
    tournament_name: Text
    tour: Tour
    surface: Literal["HARD", "CLAY", "GRASS"] | None = None
    draw_type: Literal["SINGLES"] = "SINGLES"
    best_of: Literal[3] = 3
    scheduled_start: Timestamp
    players: tuple[PlayerRef, PlayerRef]

    @model_validator(mode="after")
    def distinct_players(self) -> Self:
        if self.players[0].player_id == self.players[1].player_id:
            raise ValueError("A match requires two distinct players")
        return self

    def player(self, player_id: UUID | None) -> PlayerRef | None:
        return next((item for item in self.players if item.player_id == player_id), None)

    def opponent(self, player_id: UUID | None) -> PlayerRef | None:
        if self.player(player_id) is None:
            return None
        return next(item for item in self.players if item.player_id != player_id)


class EvidenceFact(Contract):
    """One canonical feature value, oriented to the decision's selected player.

    `difference` is the signed value selection minus opponent. It exists only when both
    values exist. A fact with no value is MISSING; it is never shown as zero.
    """

    key: Identifier
    label: Text
    kind: FactKind
    unit: Annotated[str, Field(max_length=40)] = ""
    selection_value: ExactDecimal | None = None
    opponent_value: ExactDecimal | None = None
    as_of: Timestamp
    source_id: Identifier

    @model_validator(mode="after")
    def consistent(self) -> Self:
        empty = self.selection_value is None and self.opponent_value is None
        if empty != (self.kind == FactKind.MISSING):
            raise ValueError("A fact is MISSING exactly when it has no value")
        return self

    @property
    def difference(self) -> Decimal | None:
        if self.selection_value is None or self.opponent_value is None:
            return None
        return self.selection_value - self.opponent_value

    def swapped(self) -> "EvidenceFact":
        return self.model_copy(
            update={
                "selection_value": self.opponent_value,
                "opponent_value": self.selection_value,
            }
        )


class ModelComponent(Contract):
    model: VersionRef
    role: Literal["PRIMARY", "BASELINE", "CALIBRATOR", "CONSENSUS"]
    probability: Probability | None = None


class DecisionContext(Contract):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: UUID
    match: MatchSummary
    market: Market = Market.MATCH_WINNER
    account_scope: Identifier
    quote_source_id: Identifier | None = None
    quote_observed_at: Timestamp | None = None
    # F05 quote key (bookmaker, source event, market, selection) for read-time rechecks.
    quote_key: tuple[str, str, str, str] | None = None
    source_ids: tuple[Identifier, ...]
    facts: tuple[EvidenceFact, ...] = ()
    components: tuple[ModelComponent, ...] = ()
    mode: Mode = Mode.SHADOW

    @model_validator(mode="after")
    def sources_listed(self) -> Self:
        if not self.source_ids or len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("A context requires unique source IDs")
        needed = {fact.source_id for fact in self.facts}
        if self.quote_source_id is not None:
            needed.add(self.quote_source_id)
        if needed - set(self.source_ids):
            raise ValueError("Every fact and quote source must be listed in source_ids")
        if (self.quote_source_id is None) != (self.quote_observed_at is None):
            raise ValueError("A quote source and its observation time go together")
        if len({fact.key for fact in self.facts}) != len(self.facts):
            raise ValueError("Fact keys must be unique")
        return self


class StoredDecision(Contract):
    record: DecisionRecord
    context: DecisionContext

    @model_validator(mode="after")
    def aligned(self) -> Self:
        record, context = self.record, self.context
        if record.decision_id != context.decision_id:
            raise ValueError("The context belongs to another decision")
        if record.match_id is not None and record.match_id != context.match.match_id:
            raise ValueError("The context describes another match")
        if (
            record.selection_player_id is not None
            and context.match.player(record.selection_player_id) is None
        ):
            raise ValueError("The selected player is not a match participant")
        if (record.quote_id is None) != (context.quote_observed_at is None):
            raise ValueError("A quote needs an observation time, and only a quote has one")
        return self

    @property
    def scheduled_start(self) -> datetime:
        return self.context.match.scheduled_start


class ReasonView(Contract):
    gate: str
    code: ReasonCode | None
    details: tuple[str, ...]
    text: str


class Statement(Contract):
    """One explanation sentence. DERIVED means deterministic payout or value arithmetic."""

    kind: Literal["OBSERVED", "INFERRED", "MISSING", "DERIVED", "WITHHELD", "DECISION"]
    text: str
    fact_key: str | None = None
    source_id: str | None = None


class NarrativeSentence(Contract):
    """One F14.6 agent sentence and the indexes of the statements that it cites."""

    text: str
    statement_indexes: tuple[int, ...]


class AgentProvenance(Contract):
    role_version: str
    prompt_sha256: str
    model_id: str
    trace_id: UUID
    status: str


class ExplanationView(Contract):
    """F14.6 explanation. `statements` is always the deterministic F14.5 text.

    `narrative` exists only when the explanation agent passed verification. The decision
    fields come from the read-time view, so a narrative cannot make a record actionable.
    """

    recommendation_id: UUID
    generated_at: datetime
    recorded_decision: RecommendationStatus
    decision: RecommendationStatus
    actionable: bool
    read_time_reasons: tuple[str, ...]
    statements: tuple[Statement, ...]
    source: Literal["AGENT", "DETERMINISTIC"]
    narrative: tuple[NarrativeSentence, ...] | None
    fallback_reason: str | None
    agent: AgentProvenance | None
    notice: str


class FormatView(Contract):
    tour: Tour
    draw_type: Literal["SINGLES"]
    best_of: Literal[3]


class RecommendationView(Contract):
    """Read-time view. `decision` is the effective decision; `recorded_decision` is stored.

    `recommended_stake` is positive only for an actionable BET. Unavailable metrics are
    null, never zero. Money, odds and probabilities serialize as decimal strings.
    """

    recommendation_id: UUID
    version: int
    supersedes: UUID | None
    decision_key: str
    match_id: UUID
    event: str
    tournament: str
    format: FormatView
    scheduled_start: datetime
    bookmaker: str | None
    market: Market
    selection_player_id: UUID | None
    selection: str | None
    displayed_odds: Decimal | None
    odds_withheld: bool
    recorded_decision: RecommendationStatus
    decision: RecommendationStatus
    actionable: bool
    recommended_stake: Money
    recorded_stake: Money
    maximum_stake: Money | None
    cash_return_if_win: Money | None
    probability: Decimal | None
    probability_low: Decimal | None
    probability_semantics: str | None
    break_even_probability: Decimal | None
    expected_value: Decimal | None
    expected_roi: Decimal | None
    conservative_expected_value: Decimal | None
    conservative_roi: Decimal | None
    probability_edge: Decimal | None
    generated_at: datetime
    quote_observed_at: datetime | None
    quote_age_seconds: int | None
    expires_at: datetime
    expired: bool
    superseded: bool
    failed_gates: tuple[str, ...]
    reasons: tuple[ReasonView, ...]
    read_time_reasons: tuple[str, ...]
    policy_versions: tuple[str, ...]
    payout_source: str | None
    mode: Mode
    virtual: Literal[True] = True
    manual_quote_confirmation_required: Literal[True] = True
    automated_placement: Literal[False] = False

    @model_validator(mode="after")
    def stake_rules(self) -> Self:
        if self.recommended_stake.amount != 0 and not (
            self.actionable and self.decision == RecommendationStatus.BET
        ):
            raise ValueError("Only an actionable BET carries a recommended stake")
        if self.decision != RecommendationStatus.BET and self.actionable:
            raise ValueError("Only a BET can be actionable")
        return self


class ResponsibleUseStatus(Contract):
    account_scope: str
    allowed: bool
    reason: str
    policy_version: str | None


class SourceHealth(Contract):
    source_id: str
    status: Literal["OK", "STALE", "DISABLED", "UNKNOWN"]
    reason: str
    latest_observation_at: datetime | None
    age_seconds: int | None


class RecommendationPage(Contract):
    generated_at: datetime
    view: Literal["current", "history"]
    mode: Mode
    responsible_use: ResponsibleUseStatus
    notice: str
    recommendations: tuple[RecommendationView, ...]
    next_cursor: str | None


class QuotePoint(Contract):
    bookmaker: str
    selection_player_id: UUID
    decimal_odds: Decimal | None
    odds_withheld: bool
    observed_at: datetime
    recommendation_id: UUID


class ComparisonRow(Contract):
    key: str
    label: str
    kind: FactKind | Literal["WITHHELD"]
    unit: str
    first_value: Decimal | None
    second_value: Decimal | None
    difference: Decimal | None
    as_of: datetime
    source_id: str


class MatchAnalysis(Contract):
    generated_at: datetime
    match: MatchSummary
    mode: Mode
    comparison: tuple[ComparisonRow, ...]
    components: tuple[ModelComponent, ...]
    recommendations: tuple[RecommendationView, ...]
    decision_quotes: tuple[QuotePoint, ...]
    explanations: dict[str, tuple[Statement, ...]]
    source_health: tuple[SourceHealth, ...]


class SourceRights(Contract):
    source_id: str
    redistribution_allowed: bool


class AuditView(Contract):
    """Original stored decision, unchanged. Raw payloads are never returned, only hashes."""

    generated_at: datetime
    record: DecisionRecord
    context: DecisionContext
    version_chain: tuple[UUID, ...]
    superseded_by: tuple[UUID, ...]
    source_rights: tuple[SourceRights, ...]
    raw_payloads_included: Literal[False] = False
    internal_use_only: Literal[True] = True
