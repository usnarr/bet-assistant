"""Stable transport-independent contracts for the planned feature boundaries."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import (
    Contract,
    Digest,
    ExactDecimal,
    Identifier,
    Money,
    Probability,
    ReasonCode,
    Timestamp,
    VersionRef,
)


class Tour(StrEnum):
    ATP = "ATP"
    WTA = "WTA"


class Market(StrEnum):
    MATCH_WINNER = "TENNIS_MATCH_WINNER"


class RecommendationStatus(StrEnum):
    BET = "BET"
    WATCH = "WATCH"
    NO_BET = "NO_BET"


class RawIngestionRecord(Contract):
    schema_version: Literal["1.0"] = "1.0"
    object_id: UUID
    source_id: Identifier
    observed_at: Timestamp
    effective_at: Timestamp | None = None
    source_version: Identifier
    media_type: Annotated[str, Field(min_length=1)]
    size_bytes: Annotated[int, Field(ge=0, strict=True)]
    content_sha256: Digest


class CanonicalMatch(Contract):
    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    tournament_id: UUID
    player_ids: tuple[UUID, UUID]
    scheduled_start: Timestamp
    tour: Tour
    draw_type: Literal["SINGLES"] = "SINGLES"
    best_of: Literal[3] = 3
    status: Literal["SCHEDULED", "STARTED", "FINISHED", "CANCELLED"]
    identity_reviewed: bool = False

    @model_validator(mode="after")
    def players_are_distinct(self) -> Self:
        if self.player_ids[0] == self.player_ids[1]:
            raise ValueError("A match requires two distinct players")
        return self


class QuoteObservation(Contract):
    schema_version: Literal["1.0"] = "1.0"
    quote_id: UUID
    match_id: UUID
    bookmaker: Identifier
    market: Market
    selection_player_id: UUID
    decimal_odds: Annotated[ExactDecimal, Field(gt=1)]
    observed_at: Timestamp
    valid_until: Timestamp
    raw_object_id: UUID
    policy_version: Identifier

    @model_validator(mode="after")
    def interval_is_nonempty(self) -> Self:
        if self.valid_until <= self.observed_at:
            raise ValueError("Quote validity must end after observation")
        return self


FeatureValue = Decimal | int | bool | str | None


class FeatureVector(Contract):
    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    as_of: Timestamp
    feature_set: VersionRef
    values: dict[Identifier, FeatureValue]
    input_hashes: tuple[Digest, ...]


class ProbabilityOutput(Contract):
    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    generated_at: Timestamp
    player_probabilities: dict[UUID, Probability]
    conservative_probability: Probability
    model: VersionRef
    feature_vector_sha256: Digest

    @model_validator(mode="after")
    def binary_distribution(self) -> Self:
        if len(self.player_probabilities) != 2:
            raise ValueError("Match-winner output requires exactly two players")
        if sum(self.player_probabilities.values(), Decimal(0)) != Decimal(1):
            raise ValueError("Player probabilities must sum exactly to one")
        return self


class PayoutResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    stake: Money
    cash_return_if_win: Money
    cash_return_if_loss: Money
    expected_value: Money
    break_even_probability: Probability
    policy: VersionRef

    @model_validator(mode="after")
    def currencies_match(self) -> Self:
        currencies = {
            self.stake.currency,
            self.cash_return_if_win.currency,
            self.cash_return_if_loss.currency,
            self.expected_value.currency,
        }
        if len(currencies) != 1:
            raise ValueError("Payout values must use one currency")
        if (
            min(self.stake.amount, self.cash_return_if_win.amount, self.cash_return_if_loss.amount)
            < 0
        ):
            raise ValueError("Stake and cash returns cannot be negative")
        return self


class GateResult(Contract):
    gate: Identifier
    passed: bool
    reason: ReasonCode | None = None
    evidence_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def failure_has_reason(self) -> Self:
        if not self.passed and self.reason is None:
            raise ValueError("A failed gate requires a reason")
        if self.passed and self.reason is not None:
            raise ValueError("A passing gate cannot have a failure reason")
        return self


class AuditLineage(Contract):
    schema_version: Literal["1.0"] = "1.0"
    raw_hashes: tuple[Digest, ...]
    parser: VersionRef
    dataset: VersionRef
    feature_set: VersionRef
    model: VersionRef
    policies: tuple[VersionRef, ...]
    code_revision: Annotated[str, Field(min_length=7, max_length=64, pattern=r"^[0-9a-f]+$")]


class Recommendation(Contract):
    schema_version: Literal["1.0"] = "1.0"
    recommendation_id: UUID
    match_id: UUID
    status: RecommendationStatus
    created_at: Timestamp
    expires_at: Timestamp
    stake: Money
    central_probability: Probability
    conservative_probability: Probability
    expected_value: Money
    gates: tuple[GateResult, ...]
    lineage: AuditLineage

    @model_validator(mode="after")
    def decision_invariants(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("A recommendation must expire after creation")
        if self.status != RecommendationStatus.BET and self.stake.amount != 0:
            raise ValueError("WATCH and NO_BET recommendations require zero stake")
        if self.stake.amount < 0:
            raise ValueError("Stake cannot be negative")
        failed = [gate for gate in self.gates if not gate.passed]
        if self.status == RecommendationStatus.BET and failed:
            raise ValueError("BET requires every gate to pass")
        if self.status == RecommendationStatus.NO_BET and not failed:
            raise ValueError("NO_BET requires a failed gate")
        return self
