"""F09 baselines: chronological fit, support, complementarity, bootstrap and report."""

from datetime import timedelta
from decimal import Decimal

import pytest
from baseline_support import rows, synthetic, training_rows
from pydantic import ValidationError

from tennis_engine.features.contracts import snapshot_digest
from tennis_engine.features.core import swap_values
from tennis_engine.models.baselines.baseline import TrainingRow, predict, train
from tennis_engine.models.baselines.contracts import (
    BaselineKind,
    SupportStatus,
    Uncertainty,
    UncertaintyMethod,
)
from tennis_engine.models.baselines.logit import fit, sigmoid
from tennis_engine.models.baselines.report import calibration_fit, evaluate, model_card

LN2 = Decimal(2).ln()


@pytest.fixture(scope="module")
def world():
    state = synthetic()
    split = state.matches[99][1]
    cutoff = split - timedelta(hours=1)
    train_rows = training_rows(state, cutoff)
    artifacts = {
        kind: train(train_rows, kind, training_cutoff=cutoff, version="v1", bootstrap_draws=40)
        for kind in (BaselineKind.RANKING, BaselineKind.GLOBAL_ELO, BaselineKind.SURFACE_ELO)
    }
    return state, cutoff, train_rows, artifacts


def swapped(snapshot):
    values = swap_values(snapshot.values)
    pair = (snapshot.player_ids[1], snapshot.player_ids[0])
    sha = snapshot_digest(
        snapshot.match_id,
        snapshot.as_of.isoformat(),
        snapshot.feature_set,
        snapshot.feature_set_sha256,
        snapshot.mode,
        values,
        snapshot.missing and tuple(name for name in values if values[name] is None),
        snapshot.inputs,
    )
    return snapshot.model_copy(
        update={
            "values": values,
            "player_ids": pair,
            "missing": tuple(name for name in values if values[name] is None),
            "snapshot_sha256": sha,
        }
    )


def test_logit_fit_and_symmetry():
    assert sigmoid(Decimal(0)) == Decimal("0.5")
    x = Decimal("1.7")
    assert abs(sigmoid(x) + sigmoid(-x) - 1) < Decimal("1e-35")
    result = fit([Decimal(1), Decimal(-1), Decimal(2), Decimal(-2)], [1, 0, 1, 1], l2=Decimal(1))
    assert result.converged and result.coefficient > 0
    with pytest.raises(ValueError):
        fit([], [], l2=Decimal(1))
    with pytest.raises(ValueError):
        fit([Decimal(1)], [2], l2=Decimal(1))


def test_training_is_chronological_and_reproducible(world):
    state, cutoff, train_rows, artifacts = world
    elo = artifacts[BaselineKind.GLOBAL_ELO]
    assert elo.training_cutoff == cutoff
    assert elo.coefficient > 0
    assert elo.training_rows == len(train_rows)
    again = train(
        train_rows,
        BaselineKind.GLOBAL_ELO,
        training_cutoff=cutoff,
        version="v1",
        bootstrap_draws=40,
    )
    assert again == elo
    late = [TrainingRow(item.snapshot, item.label) for item in training_rows(state, cutoff)]
    with pytest.raises(ValueError, match="training cutoff"):
        train(
            late, BaselineKind.GLOBAL_ELO, training_cutoff=cutoff - timedelta(days=3), version="v1"
        )
    with pytest.raises(ValueError, match="BLOCKED"):
        train([], BaselineKind.RANKING, training_cutoff=cutoff, version="v1")


def test_predictions_record_cutoffs_and_are_complementary(world):
    state, cutoff, _, artifacts = world
    snapshot = rows(state)[120][0]
    for artifact in artifacts.values():
        prediction = predict(snapshot, artifact, predicted_at=snapshot.as_of)
        assert prediction.training_cutoff == cutoff <= prediction.as_of
        assert prediction.feature_set == "core-v1" and not prediction.calibrated
        mirror = predict(swapped(snapshot), artifact, predicted_at=snapshot.as_of)
        if prediction.probability_player_one is None:
            assert mirror.probability_player_one is None
            continue
        total = prediction.probability_player_one + mirror.probability_player_one
        assert abs(total - 1) <= Decimal("2e-9")
        assert prediction.probability_player_two == 1 - prediction.probability_player_one


def test_support_rules_block_missing_inputs_and_flag_sparse_history(world):
    state, _, _, artifacts = world
    early = rows(state)[0][0]
    with pytest.raises(ValidationError, match="Training data must end"):
        predict(early, artifacts[BaselineKind.GLOBAL_ELO], predicted_at=early.as_of)
    early_cutoff = state.matches[20][1] - timedelta(hours=1)
    early_elo = train(
        training_rows(state, early_cutoff),
        BaselineKind.GLOBAL_ELO,
        training_cutoff=early_cutoff,
        version="v0",
        bootstrap_draws=0,
    )
    early = rows(state)[20][0]
    artifacts = artifacts | {BaselineKind.GLOBAL_ELO: early_elo}
    elo = predict(early, early_elo, predicted_at=early.as_of)
    assert elo.support == SupportStatus.SPARSE and elo.reasons == ("sparse_rating_history",)
    assert elo.probability_player_one is not None
    assert elo.uncertainty.method == UncertaintyMethod.NONE
    no_rank = early.model_copy(update={"values": early.values | {"diff.log_rank": None}})
    no_rank = swapped(swapped(no_rank))
    early_rank = train(
        training_rows(state, early_cutoff),
        BaselineKind.RANKING,
        training_cutoff=early_cutoff,
        version="v0",
        bootstrap_draws=0,
    )
    ranking = predict(no_rank, early_rank, predicted_at=early.as_of)
    assert ranking.support == SupportStatus.UNSUPPORTED
    assert ranking.probability_player_one is None and "missing_log_rank" in ranking.reasons
    five = early.model_copy(update={"values": early.values | {"match.best_of": "BEST_OF_5"}})
    five = swapped(swapped(five))
    result = predict(five, artifacts[BaselineKind.GLOBAL_ELO], predicted_at=early.as_of)
    assert result.support == SupportStatus.UNSUPPORTED and "unsupported_format" in result.reasons


def test_bootstrap_interval_is_labelled_and_brackets_the_estimate(world):
    state, _, _, artifacts = world
    snapshot = rows(state)[130][0]
    prediction = predict(snapshot, artifacts[BaselineKind.GLOBAL_ELO], predicted_at=snapshot.as_of)
    uncertainty = prediction.uncertainty
    assert uncertainty.method == UncertaintyMethod.WEEK_BLOCK_BOOTSTRAP
    assert uncertainty.draws >= 2 and uncertainty.level == Decimal("0.9")
    assert uncertainty.lower <= prediction.probability_player_one <= uncertainty.upper
    assert any("Not a confidence interval" in item for item in uncertainty.limitations)
    with pytest.raises(ValidationError):
        Uncertainty(method=UncertaintyMethod.NONE, lower=Decimal("0.4"), limitations=())


def test_report_metrics_model_card_and_out_of_sample_rule(world, tmp_path):
    state, cutoff, _, artifacts = world
    final = state.matches[-1][1] + timedelta(days=1)
    held_out = [(s, lab) for s, lab in rows(state, until=final) if s.as_of > cutoff]
    assert len(held_out) == 40
    pairs = []
    for snapshot, label in held_out:
        prediction = predict(
            snapshot, artifacts[BaselineKind.GLOBAL_ELO], predicted_at=snapshot.as_of
        )
        pairs.append((prediction, label, {"tour": "atp", "surface": "hard"}))
    metrics, unsupported = evaluate(pairs)
    overall = next(item for item in metrics if item.segment == "all")
    assert overall.count == 40 and unsupported == 0
    assert overall.log_loss is not None and overall.log_loss < LN2
    assert Decimal(0) <= overall.brier <= Decimal("0.25")
    card = model_card(
        artifacts[BaselineKind.GLOBAL_ELO],
        evaluation=metrics,
        unsupported_rows=unsupported,
        data_availability="PROSPECTIVE",
        code_revision="0123456789abcdef",
        dependency_lock_sha256="a" * 64,
        rollback_target=None,
        license_notes="Synthetic fixture data only",
    )
    assert card.status == "SHADOW_CANDIDATE" and card.evaluation_rows == 40
    assert card.parameters["coefficient"] == str(artifacts[BaselineKind.GLOBAL_ELO].coefficient)
    leaked = [(pairs[0][0], pairs[0][1].model_copy(update={"observed_at": pairs[0][0].as_of}), {})]
    with pytest.raises(ValueError, match="out-of-sample"):
        evaluate(leaked)

    from tennis_engine.models.baselines.storage import read_baseline, write_baseline

    directory = write_baseline(tmp_path, artifacts[BaselineKind.GLOBAL_ELO], card)
    assert write_baseline(tmp_path, artifacts[BaselineKind.GLOBAL_ELO], card) == directory
    assert read_baseline(directory) == (artifacts[BaselineKind.GLOBAL_ELO], card)
    other = card.model_copy(update={"license_notes": "changed"})
    with pytest.raises(FileExistsError):
        write_baseline(tmp_path, artifacts[BaselineKind.GLOBAL_ELO], other)
    with pytest.raises(ValueError, match="different artifact"):
        write_baseline(tmp_path, artifacts[BaselineKind.RANKING], card)


def test_calibration_fit_recovers_identity_on_calibrated_data():
    probabilities, outcomes = [], []
    for index in range(1, 10):
        p = Decimal(index) / Decimal(10)
        wins = index
        probabilities += [p] * 10
        outcomes += [1] * wins + [0] * (10 - wins)
    intercept, slope = calibration_fit(probabilities, outcomes)
    assert abs(intercept) < Decimal("0.05") and abs(slope - 1) < Decimal("0.05")
    assert calibration_fit([Decimal("0.5")] * 3, [1, 0, 1]) is None
