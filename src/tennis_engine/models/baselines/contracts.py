"""F09 baseline model contracts: predictions, uncertainty, artifacts and model cards."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, Digest, Identifier, Probability, Timestamp


class SupportStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    SPARSE = "SPARSE"
    UNSUPPORTED = "UNSUPPORTED"


class UncertaintyMethod(StrEnum):
    WEEK_BLOCK_BOOTSTRAP = "WEEK_BLOCK_BOOTSTRAP"
    NONE = "NONE"


class Uncertainty(Contract):
    """Spread of refitted model probabilities. It is not a confidence interval for the
    unknown true win probability, and it ignores feature and data uncertainty."""

    method: UncertaintyMethod
    level: Probability | None = None
    lower: Probability | None = None
    upper: Probability | None = None
    draws: Annotated[int, Field(ge=0, strict=True)] = 0
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def complete(self) -> Self:
        present = (self.level, self.lower, self.upper)
        if self.method == UncertaintyMethod.NONE:
            if any(item is not None for item in present) or self.draws:
                raise ValueError("NONE uncertainty cannot carry an interval")
        elif any(item is None for item in present) or self.draws < 2:
            raise ValueError("A bootstrap interval needs a level, bounds and at least 2 draws")
        elif self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("Lower bound exceeds upper bound")
        return self


class BaselineKind(StrEnum):
    RANKING = "RANKING"
    GLOBAL_ELO = "GLOBAL_ELO"
    SURFACE_ELO = "SURFACE_ELO"


class BaselineArtifact(Contract):
    """Immutable fitted baseline. ``probability = sigmoid(coefficient * x)``."""

    schema_version: Literal["1.0"] = "1.0"
    model_id: UUID
    name: Identifier
    version: Identifier
    kind: BaselineKind
    feature: Identifier
    feature_sign: Literal[1, -1]
    coefficient: Decimal
    l2_penalty: Decimal
    bootstrap_coefficients: tuple[Decimal, ...]
    bootstrap_level: Probability
    min_support_matches: Annotated[int, Field(ge=0, strict=True)]
    supported_best_of: tuple[Annotated[str, Field(pattern=r"^BEST_OF_[35]$")], ...]
    feature_set: Identifier
    feature_set_sha256: Digest
    dataset_id: UUID | None
    training_cutoff: Timestamp
    training_rows: Annotated[int, Field(ge=1, strict=True)]
    converged: bool
    seed: int
    artifact_sha256: Digest

    @model_validator(mode="after")
    def converged_fit(self) -> Self:
        if not self.converged:
            raise ValueError("A non-converged fit cannot become an artifact")
        return self


class BaselinePrediction(Contract):
    """Raw (uncalibrated) baseline output for canonical player one."""

    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    player_ids: tuple[UUID, UUID]
    model: Identifier
    model_version: Identifier
    artifact_sha256: Digest
    feature_set: Identifier
    snapshot_sha256: Digest
    as_of: Timestamp
    predicted_at: Timestamp
    training_cutoff: Timestamp
    support: SupportStatus
    reasons: tuple[Identifier, ...] = ()
    calibrated: Literal[False] = False
    probability_player_one: Probability | None
    components: dict[Identifier, Decimal | int | None]
    uncertainty: Uncertainty

    @model_validator(mode="after")
    def unsupported_has_no_probability(self) -> Self:
        if (self.support == SupportStatus.UNSUPPORTED) != (self.probability_player_one is None):
            raise ValueError("Only an unsupported prediction lacks a probability")
        if self.support != SupportStatus.SUPPORTED and not self.reasons:
            raise ValueError("A sparse or unsupported prediction needs a reason")
        if self.predicted_at < self.as_of:
            raise ValueError("A prediction cannot be made before its feature cutoff")
        if self.training_cutoff > self.as_of:
            raise ValueError("Training data must end at or before the prediction cutoff")
        return self

    @property
    def probability_player_two(self) -> Decimal | None:
        if self.probability_player_one is None:
            return None
        return Decimal(1) - self.probability_player_one


class SegmentMetrics(Contract):
    segment: Annotated[str, Field(min_length=1, max_length=128)]
    count: Annotated[int, Field(ge=0, strict=True)]
    log_loss: Decimal | None
    brier: Decimal | None
    accuracy: Decimal | None
    calibration_intercept: Decimal | None
    calibration_slope: Decimal | None
    mean_probability: Decimal | None
    event_rate: Decimal | None


class ModelCard(Contract):
    schema_version: Literal["1.0"] = "1.0"
    model: Identifier
    model_version: Identifier
    artifact_sha256: Digest
    status: Literal["SHADOW_CANDIDATE", "RESEARCH_ONLY"]
    supported_scope: Annotated[str, Field(min_length=1)]
    training_cutoff: Timestamp
    dataset_id: UUID | None
    data_availability: Identifier
    feature_set: Identifier
    feature_set_sha256: Digest
    code_revision: Annotated[str, Field(min_length=7, max_length=64, pattern=r"^[0-9a-f]+$")]
    dependency_lock_sha256: Digest
    parameters: dict[Identifier, str]
    evaluation: tuple[SegmentMetrics, ...]
    evaluation_rows: int
    unsupported_rows: int
    uncertainty_limitations: tuple[str, ...]
    known_limitations: tuple[str, ...]
    rollback_target: Identifier | None
    license_notes: Annotated[str, Field(min_length=1)]
