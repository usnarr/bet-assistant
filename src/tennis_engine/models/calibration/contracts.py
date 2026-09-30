"""F11 calibrator artifacts and calibrated predictions (F11.5, F11.7).

A calibrated prediction keeps the reference to its raw base prediction. Its spread is the
base bootstrap spread mapped through the calibrator. It is not a confidence interval for
the unknown true win probability, and it excludes calibrator fit uncertainty.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import (
    Contract,
    Digest,
    Identifier,
    Probability,
    Timestamp,
    VersionRef,
)
from tennis_engine.models.baselines.contracts import SupportStatus, Uncertainty

Count = Annotated[int, Field(ge=0, strict=True)]


class CalibrationMethod(StrEnum):
    """Both methods are symmetric: ``f(1 - p) = 1 - f(p)``, because player order is
    arbitrary. A symmetric beta calibration (``a = b``, ``c = 0``) equals symmetric Platt,
    so it is not a separate method."""

    PLATT_SYMMETRIC = "PLATT_SYMMETRIC"
    ISOTONIC_SYMMETRIC = "ISOTONIC_SYMMETRIC"


class Knot(Contract):
    raw: Probability
    calibrated: Probability


class CalibrationTrial(Contract):
    """One method fitted on the fit part and scored on the later validation part."""

    method: CalibrationMethod
    fit_rows: Count
    validation_rows: Count
    raw_log_loss: Decimal
    raw_brier: Decimal
    log_loss: Decimal
    brier: Decimal


class CalibratorArtifact(Contract):
    schema_version: Literal["1.0"] = "1.0"
    calibrator_id: UUID
    name: Identifier
    version: Identifier
    method: CalibrationMethod
    base_model: Identifier
    base_model_version: Identifier
    base_artifact_sha256: Digest
    base_training_cutoff: Timestamp
    window_start: Timestamp
    validation_start: Timestamp
    window_end: Timestamp
    rows: Annotated[int, Field(ge=1, strict=True)]
    slope: Decimal | None
    knots: tuple[Knot, ...]
    min_probability: Probability
    trials: tuple[CalibrationTrial, ...]
    selection_rule: Literal["lowest-validation-log-loss;tie:platt"] = (
        "lowest-validation-log-loss;tie:platt"
    )
    artifact_sha256: Digest

    @model_validator(mode="after")
    def disjoint_and_complete(self) -> Self:
        if not (
            self.base_training_cutoff < self.window_start < self.validation_start < self.window_end
        ):
            raise ValueError(
                "The calibration window must start after the training cutoff and "
                "contain a later validation part"
            )
        if (self.method == CalibrationMethod.PLATT_SYMMETRIC) != (self.slope is not None):
            raise ValueError("Only a Platt calibrator has a slope")
        if self.method == CalibrationMethod.PLATT_SYMMETRIC and self.slope is not None:
            if self.slope <= 0:
                raise ValueError("A non-positive slope reverses or flattens the ranking")
        if (self.method == CalibrationMethod.ISOTONIC_SYMMETRIC) != bool(self.knots):
            raise ValueError("Only an isotonic calibrator has knots")
        if any(
            later.raw <= earlier.raw or later.calibrated < earlier.calibrated
            for earlier, later in zip(self.knots, self.knots[1:], strict=False)
        ):
            raise ValueError("Isotonic knots must be increasing")
        if self.min_probability >= Decimal("0.5"):
            raise ValueError("The probability floor must be below one half")
        if not self.trials:
            raise ValueError("A calibrator records its method comparison")
        return self


class CalibratedPrediction(Contract):
    """Calibrated output for canonical player one. F12 reads this like a baseline."""

    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    player_ids: tuple[UUID, UUID]
    model: Identifier
    model_version: Identifier
    artifact_sha256: Digest
    calibrator: VersionRef
    feature_set: Identifier
    snapshot_sha256: Digest
    as_of: Timestamp
    predicted_at: Timestamp
    training_cutoff: Timestamp
    calibration_cutoff: Timestamp
    support: SupportStatus
    reasons: tuple[Identifier, ...] = ()
    calibrated: Literal[True] = True
    raw_probability_player_one: Probability | None
    probability_player_one: Probability | None
    uncertainty: Uncertainty

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.support == SupportStatus.UNSUPPORTED) != (self.probability_player_one is None):
            raise ValueError("Only an unsupported prediction lacks a probability")
        if (self.raw_probability_player_one is None) != (self.probability_player_one is None):
            raise ValueError("A calibrated probability needs its raw probability")
        if self.support != SupportStatus.SUPPORTED and not self.reasons:
            raise ValueError("A sparse or unsupported prediction needs a reason")
        if self.calibration_cutoff > self.as_of:
            raise ValueError("The calibrator must be fitted before the prediction cutoff")
        if self.predicted_at < self.as_of:
            raise ValueError("A prediction cannot be made before its feature cutoff")
        return self

    @property
    def model_ref(self) -> VersionRef:
        """The scoring bundle: base model name, calibrator version and calibrator hash."""
        return VersionRef(
            component=self.model,
            version=self.calibrator.version,
            sha256=self.calibrator.sha256,
        )
