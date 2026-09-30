from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pricing_support import decision_policy
from settlement_support import MATCH_ID, PLAYER_A, PLAYER_B
from test_pricing_decision import AT, SHA, failed, inputs

from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.features.contracts import digest
from tennis_engine.models.baselines.contracts import (
    BaselinePrediction,
    SupportStatus,
    Uncertainty,
    UncertaintyMethod,
)
from tennis_engine.models.baselines.market import ConsensusOutput
from tennis_engine.models.calibration.calibrate import calibrate
from tennis_engine.models.calibration.contracts import (
    CalibrationMethod,
    CalibrationTrial,
    CalibratorArtifact,
)
from tennis_engine.pricing.decision import Gate, decide
from tennis_engine.pricing.model_input import assessment_from_baselines, consensus_for_selection

D = Decimal
ORDER = (PLAYER_A, PLAYER_B)


def prediction(p1="0.60", lower="0.56", upper="0.64", model="baseline-surface-elo", **overrides):
    spread = (
        Uncertainty(
            method=UncertaintyMethod.WEEK_BLOCK_BOOTSTRAP,
            level=D("0.9"),
            lower=D(lower),
            upper=D(upper),
            draws=200,
            limitations=("model spread, not a confidence interval",),
        )
        if lower is not None
        else Uncertainty(method=UncertaintyMethod.NONE, limitations=("none",))
    )
    return BaselinePrediction.model_validate(
        {
            "match_id": MATCH_ID,
            "player_ids": ORDER,
            "model": model,
            "model_version": f"{model}-v1",
            "artifact_sha256": SHA,
            "feature_set": "core-v1",
            "snapshot_sha256": SHA,
            "as_of": AT - timedelta(minutes=5),
            "predicted_at": AT - timedelta(minutes=4),
            "training_cutoff": AT - timedelta(days=1),
            "support": SupportStatus.SUPPORTED,
            "probability_player_one": D(p1) if p1 is not None else None,
            "components": {},
            "uncertainty": spread,
        }
        | overrides
    )


def assess(primary, others=(), selection=PLAYER_A):
    return assessment_from_baselines(
        primary, others, match_id=MATCH_ID, selection_player_id=selection
    )


def test_selection_side_uses_canonical_order_and_mirrors_the_spread():
    first = assess(prediction(), [prediction("0.58", model="baseline-global-elo")])
    assert first.probability == D("0.60") and first.conservative_probability == D("0.56")
    assert first.disagreement == D("0.02") and first.semantics == "SPORTING_WIN"
    second = assess(prediction(), selection=PLAYER_B)
    assert second.probability == D("0.40") and second.conservative_probability == D("0.36")
    with pytest.raises(ValueError, match="not a player"):
        assess(prediction(), selection=UUID(int=3))
    with pytest.raises(ValueError, match="match"):
        assess(prediction(match_id=UUID(int=9)))


def test_missing_probability_or_spread_gives_no_assessment():
    unsupported = prediction(p1=None, support=SupportStatus.UNSUPPORTED, reasons=("missing_input",))
    assert assess(unsupported) is None
    assert assess(prediction(lower=None)) is None


def test_sparse_primary_and_lone_baseline_fail_their_gates():
    sparse = assess(prediction(support=SupportStatus.SPARSE, reasons=("sparse",)))
    assert sparse.in_supported_domain is False and sparse.disagreement is None


def test_raw_f09_baselines_can_never_produce_a_bet():
    stressed = decision_policy(void_stress_probability="0.02")
    model = assess(prediction(), [prediction("0.59", model="baseline-global-elo")])
    record = decide(inputs(model=model, decision_policy=stressed))
    assert record.status == RecommendationStatus.NO_BET
    assert set(failed(record)) >= {Gate.MODEL_CALIBRATED}
    assert Gate.MODEL_DOMAIN not in failed(record)
    assert Gate.MODEL_DISAGREEMENT not in failed(record)
    assert record.stake.amount == 0


def calibrator(slope="1"):
    body = {
        "version": "cal-v1",
        "method": CalibrationMethod.PLATT_SYMMETRIC.value,
        "slope": slope,
    }
    return CalibratorArtifact(
        calibrator_id=UUID(int=55),
        name="baseline-surface-elo-calibrator",
        version="cal-v1",
        method=CalibrationMethod.PLATT_SYMMETRIC,
        base_model="baseline-surface-elo",
        base_model_version="baseline-surface-elo-v1",
        base_artifact_sha256=SHA,
        base_training_cutoff=AT - timedelta(days=30),
        window_start=AT - timedelta(days=29),
        validation_start=AT - timedelta(days=10),
        window_end=AT - timedelta(days=1),
        rows=100,
        slope=D(slope),
        knots=(),
        min_probability=D("0.01"),
        trials=(
            CalibrationTrial(
                method=CalibrationMethod.PLATT_SYMMETRIC,
                fit_rows=60,
                validation_rows=40,
                raw_log_loss=D("0.66"),
                raw_brier=D("0.23"),
                log_loss=D("0.65"),
                brier=D("0.23"),
            ),
        ),
        artifact_sha256=digest(body),
    )


def calibrated(raw, artifact=None):
    return calibrate(raw, artifact or calibrator(), predicted_at=raw.predicted_at)


def test_calibrated_f11_output_passes_the_calibration_gate_and_can_bet():
    stressed = decision_policy(void_stress_probability="0.02")
    artifact = calibrator()
    model = assess(
        calibrated(prediction(training_cutoff=AT - timedelta(days=30)), artifact),
        [prediction("0.59", model="baseline-global-elo")],
    )
    assert model.calibrated is True
    assert model.model.sha256 == artifact.artifact_sha256
    record = decide(inputs(model=model, decision_policy=stressed))
    assert not failed(record)
    assert record.status == RecommendationStatus.BET and record.stake.amount > 0


def test_a_shrinking_calibrator_can_remove_the_edge():
    stressed = decision_policy(void_stress_probability="0.02")
    shrunk = calibrated(prediction(training_cutoff=AT - timedelta(days=30)), calibrator("0.2"))
    model = assess(shrunk, [prediction("0.59", model="baseline-global-elo")])
    assert model.probability < D("0.60")
    record = decide(inputs(model=model, decision_policy=stressed))
    assert Gate.MODEL_CALIBRATED not in failed(record)
    assert record.status != RecommendationStatus.BET and record.stake.amount == 0


def consensus(p1="0.58", support=SupportStatus.SUPPORTED):
    return ConsensusOutput(
        match_id=MATCH_ID,
        player_ids=ORDER,
        as_of=AT,
        method="logit-mean",
        support=support,
        reasons=() if support == SupportStatus.SUPPORTED else ("single_bookmaker",),
        probability_player_one=D(p1),
        pairs=(),
        weights=(),
        rejected={},
    )


def test_only_supported_consensus_confirms_an_edge():
    assert consensus_for_selection(
        consensus(), match_id=MATCH_ID, selection_player_id=PLAYER_B
    ) == D("0.42")
    assert (
        consensus_for_selection(
            consensus(support=SupportStatus.SPARSE),
            match_id=MATCH_ID,
            selection_player_id=PLAYER_A,
        )
        is None
    )
    assert consensus_for_selection(None, match_id=MATCH_ID, selection_player_id=PLAYER_A) is None
