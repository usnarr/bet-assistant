"""F11 calibration: disjoint windows, symmetric methods, bundle checks and F12 wiring."""

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tennis_engine.features.labels import MatchLabel
from tennis_engine.models.baselines.contracts import (
    BaselinePrediction,
    SupportStatus,
    Uncertainty,
    UncertaintyMethod,
)
from tennis_engine.models.calibration.calibrate import (
    Row,
    apply,
    calibrate,
    calibrate_with_set,
    fit_calibrator,
    fit_calibrator_set,
    fit_isotonic,
    is_symmetric,
)
from tennis_engine.models.calibration.contracts import (
    CalibrationMethod,
    CalibratorArtifact,
    CalibratorSet,
)
from tennis_engine.models.calibration.storage import read_bundle, write_calibrator
from tennis_engine.normalization.contracts import MatchStatus
from tennis_engine.pricing.model_input import assessment_from_baselines

D = Decimal
SHA = "a" * 64
TRAINING_CUTOFF = datetime(2026, 3, 1, tzinfo=UTC)
WINDOW_START = datetime(2026, 3, 2, tzinfo=UTC)
VALIDATION_START = datetime(2026, 5, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 6, 1, tzinfo=UTC)


def raw_prediction(
    index: int,
    p: str | Decimal | None,
    as_of: datetime,
    *,
    sha: str = SHA,
    support: SupportStatus = SupportStatus.SUPPORTED,
    spread: tuple[str, str] | None = None,
) -> BaselinePrediction:
    uncertainty = (
        Uncertainty(
            method=UncertaintyMethod.WEEK_BLOCK_BOOTSTRAP,
            level=D("0.9"),
            lower=D(spread[0]),
            upper=D(spread[1]),
            draws=200,
            limitations=("spread",),
        )
        if spread
        else Uncertainty(method=UncertaintyMethod.NONE, limitations=("none",))
    )
    return BaselinePrediction(
        match_id=UUID(int=index),
        player_ids=(UUID(int=10_000 + index), UUID(int=20_000 + index)),
        model="baseline-surface-elo",
        model_version="baseline-surface-elo-v1",
        artifact_sha256=sha,
        feature_set="core-v1",
        snapshot_sha256=SHA,
        as_of=as_of,
        predicted_at=as_of,
        training_cutoff=TRAINING_CUTOFF,
        support=support,
        reasons=() if support == SupportStatus.SUPPORTED else ("sparse",),
        probability_player_one=None if p is None else D(p),
        components={},
        uncertainty=uncertainty,
    )


def label(index: int, won: bool, observed_at: datetime) -> MatchLabel:
    return MatchLabel(
        match_id=UUID(int=index),
        player_one_won=won,
        status=MatchStatus.COMPLETED,
        result_version=1,
        corrected=False,
        observed_at=observed_at,
    )


def overconfident(count: int = 400, seed: int = 11, slope: float = 0.5):
    """Raw probabilities are too extreme: the true probability is sigmoid(slope * logit)."""
    rng = random.Random(seed)
    span = (WINDOW_END - WINDOW_START) - timedelta(hours=4)
    pairs = []
    for index in range(count):
        raw = rng.uniform(0.05, 0.95)
        truth = 1 / (1 + math.exp(-slope * math.log(raw / (1 - raw))))
        as_of = WINDOW_START + span * index / count
        pairs.append(
            (
                raw_prediction(index, D(str(round(raw, 6))), as_of),
                label(index, rng.random() < truth, as_of + timedelta(hours=3)),
            )
        )
    return pairs


def fitted(pairs=None, **kwargs) -> CalibratorArtifact:
    return fit_calibrator(
        overconfident() if pairs is None else pairs,
        window_start=WINDOW_START,
        validation_start=VALIDATION_START,
        window_end=WINDOW_END,
        version="calibrator-v1",
        **kwargs,
    )


def test_platt_shrinks_overconfident_probabilities_and_beats_raw_on_validation():
    artifact = fitted(methods=(CalibrationMethod.PLATT_SYMMETRIC,))
    assert artifact.method == CalibrationMethod.PLATT_SYMMETRIC
    assert artifact.slope is not None and D("0.3") < artifact.slope < D("0.75")
    (trial,) = artifact.trials
    assert trial.log_loss < trial.raw_log_loss
    assert apply(artifact, D("0.9")) < D("0.9") and apply(artifact, D("0.1")) > D("0.1")
    assert apply(artifact, D("0.5")) == D("0.5")


def test_method_comparison_records_every_trial_and_picks_the_lowest_log_loss():
    artifact = fitted()
    assert {trial.method for trial in artifact.trials} == set(CalibrationMethod)
    best = min(artifact.trials, key=lambda trial: trial.log_loss)
    assert artifact.method == best.method
    assert artifact.rows == 400
    assert all(trial.validation_rows > 0 and trial.fit_rows > 0 for trial in artifact.trials)


@given(st.decimals(min_value=D("0.001"), max_value=D("0.999"), places=6))
def test_both_methods_are_symmetric_and_bounded(p):
    for method in CalibrationMethod:
        artifact = CALIBRATORS[method]
        assert is_symmetric(artifact, p, D("2e-9"))
        value = apply(artifact, p)
        assert artifact.min_probability <= value <= 1 - artifact.min_probability


@given(
    st.lists(st.decimals(min_value=D("0.01"), max_value=D("0.99"), places=4), min_size=2),
)
def test_calibrators_preserve_the_ranking(values):
    ordered = sorted(values)
    for artifact in CALIBRATORS.values():
        mapped = [apply(artifact, p) for p in ordered]
        assert mapped == sorted(mapped)


def test_isotonic_pools_violations_symmetrically():
    rows = [
        Row(D("0.2"), 1, WINDOW_START),
        Row(D("0.3"), 0, WINDOW_START),
        Row(D("0.7"), 1, WINDOW_START),
        Row(D("0.8"), 1, WINDOW_START),
    ]
    knots = fit_isotonic(rows, D("0.01"))
    values = [knot.calibrated for knot in knots]
    assert values == sorted(values)
    for knot in knots:
        mirror = next(item for item in knots if item.raw == 1 - knot.raw)
        assert knot.calibrated + mirror.calibrated == 1


def test_window_overlapping_training_data_is_rejected_as_leakage():
    pairs = overconfident()
    with pytest.raises(ValueError, match="overlaps the base model's training data"):
        fit_calibrator(
            pairs,
            window_start=TRAINING_CUTOFF - timedelta(days=1),
            validation_start=VALIDATION_START,
            window_end=WINDOW_END,
            version="calibrator-v1",
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda pairs: pairs + [pairs[0]], "appears twice"),
        (
            lambda pairs: (
                pairs + [(raw_prediction(999, "0.5", WINDOW_START, sha="b" * 64), pairs[0][1])]
            ),
            "different matches|one base artifact",
        ),
        (
            lambda pairs: (
                pairs + [(raw_prediction(999, "0.5", WINDOW_END), label(999, True, WINDOW_END))]
            ),
            "outside the calibration window",
        ),
        (
            lambda pairs: (
                pairs + [(raw_prediction(999, "0.5", WINDOW_START), label(999, True, WINDOW_START))]
            ),
            "not out-of-sample",
        ),
    ],
)
def test_invalid_rows_are_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        fitted(change(overconfident()))


def test_too_few_rows_or_late_labels_block_the_calibrator():
    with pytest.raises(ValueError, match="BLOCKED"):
        fitted(overconfident(count=60))
    late = [
        (prediction, label(index, item.player_one_won, WINDOW_END + timedelta(days=1)))
        for index, (prediction, item) in enumerate(overconfident())
    ]
    with pytest.raises(ValueError, match="BLOCKED"):
        fitted(late)


def test_sparse_rows_are_not_used_for_fitting():
    pairs = overconfident()
    sparse = [
        (
            raw_prediction(5000 + i, "0.99", WINDOW_START, support=SupportStatus.SPARSE),
            label(5000 + i, False, WINDOW_START + timedelta(hours=3)),
        )
        for i in range(50)
    ]
    assert fitted(pairs + sparse).rows == 400


def test_calibrate_maps_the_spread_and_refuses_a_mismatched_bundle():
    artifact = CALIBRATORS[CalibrationMethod.PLATT_SYMMETRIC]
    later = WINDOW_END + timedelta(days=2)
    prediction = raw_prediction(1, "0.80", later, spread=("0.74", "0.85"))
    result = calibrate(prediction, artifact, predicted_at=later)
    assert result.calibrated is True and result.raw_probability_player_one == D("0.80")
    assert result.uncertainty.lower is not None and result.uncertainty.upper is not None
    assert result.uncertainty.lower < result.probability_player_one < result.uncertainty.upper
    assert result.probability_player_one < D("0.80")
    assert result.model_ref.sha256 == artifact.artifact_sha256
    with pytest.raises(ValueError, match="different base artifact"):
        calibrate(raw_prediction(1, "0.8", later, sha="b" * 64), artifact, predicted_at=later)
    with pytest.raises(ValueError, match="inside the calibration window"):
        calibrate(raw_prediction(1, "0.8", VALIDATION_START), artifact, predicted_at=later)
    unsupported = raw_prediction(2, None, later, support=SupportStatus.UNSUPPORTED)
    passed = calibrate(unsupported, artifact, predicted_at=later)
    assert passed.probability_player_one is None
    assert passed.uncertainty.method == UncertaintyMethod.NONE


def test_bundle_storage_round_trip_and_mismatch(tmp_path):
    artifact = CALIBRATORS[CalibrationMethod.ISOTONIC_SYMMETRIC]
    directory = write_calibrator(tmp_path, artifact, code_revision="abcdef1")
    assert write_calibrator(tmp_path, artifact, code_revision="abcdef1") == directory
    reread = CalibratorArtifact.model_validate_json((directory / "calibrator.json").read_bytes())
    assert reread == artifact
    with pytest.raises(FileNotFoundError):
        read_bundle(tmp_path / "missing", directory)


def test_contract_rejects_invalid_calibrators():
    artifact = CALIBRATORS[CalibrationMethod.PLATT_SYMMETRIC]
    body = artifact.model_dump()
    with pytest.raises(ValueError, match="non-positive slope"):
        CalibratorArtifact.model_validate(body | {"slope": D("-0.5")})
    with pytest.raises(ValueError, match="training cutoff"):
        CalibratorArtifact.model_validate(body | {"window_start": TRAINING_CUTOFF})
    with pytest.raises(ValueError, match="method comparison"):
        CalibratorArtifact.model_validate(body | {"trials": ()})


CALIBRATORS = {method: fitted(methods=(method,)) for method in CalibrationMethod}


# F11.6 calibrator bootstrap and F11.5 tour calibrators.

BOOTSTRAPPED = fitted(methods=(CalibrationMethod.PLATT_SYMMETRIC,), bootstrap_draws=60)


def test_calibrator_bootstrap_is_seeded_and_adds_fit_uncertainty():
    boot = BOOTSTRAPPED.bootstrap
    assert boot is not None and boot.requested == 60
    assert len(boot.draws) + boot.failed == 60 and len(boot.draws) >= 50
    assert boot.blocks >= 12
    slopes = sorted(draw.slope for draw in boot.draws)
    assert slopes[0] < BOOTSTRAPPED.slope < slopes[-1]
    again = fitted(methods=(CalibrationMethod.PLATT_SYMMETRIC,), bootstrap_draws=60)
    assert again == BOOTSTRAPPED
    other = fitted(
        methods=(CalibrationMethod.PLATT_SYMMETRIC,), bootstrap_draws=60, bootstrap_seed=5
    )
    assert other.artifact_sha256 != BOOTSTRAPPED.artifact_sha256
    plain = CALIBRATORS[CalibrationMethod.PLATT_SYMMETRIC]
    assert plain.bootstrap is None and plain.artifact_sha256 != BOOTSTRAPPED.artifact_sha256

    later = WINDOW_END + timedelta(days=2)
    spread = raw_prediction(1, "0.80", later, spread=("0.74", "0.85"))
    wide = calibrate(spread, BOOTSTRAPPED, predicted_at=later)
    narrow = calibrate(spread, plain, predicted_at=later)
    assert wide.uncertainty.method == UncertaintyMethod.WEEK_BLOCK_BOOTSTRAP
    assert wide.uncertainty.level == D("0.8") and wide.uncertainty.draws == len(boot.draws)
    assert wide.uncertainty.lower < narrow.uncertainty.lower
    assert wide.uncertainty.upper > narrow.uncertainty.upper
    assert "calibrator week-block draw" in wide.uncertainty.limitations[0]
    # Without a base spread, the spread comes from the calibrator draws only.
    alone = calibrate(raw_prediction(2, "0.80", later), BOOTSTRAPPED, predicted_at=later)
    assert alone.uncertainty.lower < alone.probability_player_one < alone.uncertainty.upper
    assessment = assessment_from_baselines(
        wide, [narrow], match_id=wide.match_id, selection_player_id=wide.player_ids[1]
    )
    assert assessment is not None
    assert assessment.conservative_probability == 1 - wide.uncertainty.upper


def test_calibrator_bootstrap_needs_two_draws_and_rejects_foreign_draws():
    with pytest.raises(ValueError, match="two weeks and two draws"):
        fitted(bootstrap_draws=1)
    body = BOOTSTRAPPED.model_dump()
    draws = body["bootstrap"]["draws"]
    draws = (draws[0] | {"slope": None, "knots": ({"raw": "0.2", "calibrated": "0.3"},)},)
    broken = body | {"bootstrap": body["bootstrap"] | {"draws": draws * 2, "failed": 58}}
    with pytest.raises(ValueError, match="calibrator's method"):
        CalibratorArtifact.model_validate(broken)


def tour(prediction):
    index = prediction.match_id.int
    return "challenger" if index % 25 == 0 else "wta" if index % 4 == 0 else "atp"


def test_tour_calibrators_need_enough_rows_and_fall_back_to_the_pooled_one():
    pairs = overconfident(count=900)
    calibrators = fit_calibrator_set(
        pairs,
        segment_of=tour,
        window_start=WINDOW_START,
        validation_start=VALIDATION_START,
        window_end=WINDOW_END,
        version="calibrator-set-v1",
    )
    assert set(calibrators.segments) == {"atp", "wta"}
    assert set(calibrators.skipped) == {"challenger"}
    assert "BLOCKED" in calibrators.skipped["challenger"]
    atp = calibrators.segments["atp"]
    assert atp.segment == "atp" and atp.name == "baseline-surface-elo-calibrator-atp"
    assert atp.rows < calibrators.pooled.rows
    assert atp.artifact_sha256 != calibrators.pooled.artifact_sha256
    assert calibrators.for_segment("challenger") == calibrators.pooled
    later = WINDOW_END + timedelta(days=1)
    prediction = raw_prediction(3, "0.7", later)
    used = calibrate_with_set(prediction, calibrators, segment="wta", predicted_at=later)
    assert used.calibrator.sha256 == calibrators.segments["wta"].artifact_sha256
    fallback = calibrate_with_set(prediction, calibrators, segment="itf", predicted_at=later)
    assert fallback.calibrator.sha256 == calibrators.pooled.artifact_sha256
    assert (
        fit_calibrator_set(
            pairs,
            segment_of=tour,
            window_start=WINDOW_START,
            validation_start=VALIDATION_START,
            window_end=WINDOW_END,
            version="calibrator-set-v1",
        )
        == calibrators
    )

    body = calibrators.model_dump()
    with pytest.raises(ValueError, match="its own tour"):
        CalibratorSet.model_validate(body | {"segments": {"wta": body["segments"]["atp"]}})
    with pytest.raises(ValueError, match="calibrated or skipped"):
        CalibratorSet.model_validate(body | {"skipped": {"atp": "x"}})
    with pytest.raises(ValueError, match="reserved"):
        fit_calibrator_set(
            pairs,
            segment_of=lambda _: "all",
            window_start=WINDOW_START,
            validation_start=VALIDATION_START,
            window_end=WINDOW_END,
            version="calibrator-set-v1",
        )
