"""F11.1 to F11.4 tabular model, nested tuning, player-swap symmetry and stacking.

All data is synthetic. The results show that the pipeline works, not that a model works.
"""

import math
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

import pytest
from backtest_support import START, boundaries, quote_history, world

from tennis_engine.backtesting.ensemble import StackedCandidate, TabularCandidate
from tennis_engine.backtesting.market import history_quotes
from tennis_engine.backtesting.metrics import matched, paired_log_loss_difference
from tennis_engine.backtesting.runner import BaselineCandidate, run_walk_forward
from tennis_engine.backtesting.splits import walk_forward
from tennis_engine.features.contracts import AvailabilityMode
from tennis_engine.features.core import CORE_SET, swap_values
from tennis_engine.features.dataset import build_dataset
from tennis_engine.features.labels import label_known_at
from tennis_engine.models.baselines.baseline import TrainingRow
from tennis_engine.models.baselines.contracts import BaselineKind, SupportStatus
from tennis_engine.models.baselines.market import consensus
from tennis_engine.models.tabular.booster import (
    DEFAULT_GRID,
    TabularConfig,
    _symmetric,
    load_booster,
    train_tabular,
)
from tennis_engine.models.tabular.folds import inner_folds
from tennis_engine.models.tabular.schema import MARKET_FEATURE, build_schema, encode
from tennis_engine.models.tabular.stacker import (
    OutOfFoldRow,
    StackingLeakage,
    fit_stacker,
    stack,
)

D = Decimal
SMALL = TabularConfig(
    name="tabular-xgb-small", grid=DEFAULT_GRID[:1] + DEFAULT_GRID[4:5], search_budget=2
)


@pytest.fixture(scope="module")
def data():
    state = world(count=150)
    _, snapshots = build_dataset(
        state.h.store,
        CORE_SET,
        state.rows(("24h", "1h")),
        name="tabular-synthetic",
        mode=AvailabilityMode.PROSPECTIVE,
        cutoff_rule="first known start minus 24 hours and 1 hour",
        created_at=START,
        code_revision="0000000",
        source_versions={},
    )
    split = walk_forward(snapshots, boundaries(first_day=35, days=12), name="tabular-synthetic")
    cutoff = split.folds[-1].training_cutoff
    rows = []
    for snapshot in snapshots:
        label = label_known_at(state.h.store, snapshot.match_id, cutoff)
        if snapshot.as_of < cutoff and label is not None and label.observed_at <= cutoff:
            rows.append(TrainingRow(snapshot, label))
    return state, snapshots, split, rows, cutoff


def test_inner_folds_keep_matches_together_and_train_only_on_earlier_labels(data):
    *_, rows, _ = data
    folds = inner_folds(rows, folds=3)
    owner = {}
    for fold in folds:
        for row in fold.validate:
            assert owner.setdefault(row.snapshot.match_id, fold.index) == fold.index
        assert all(row.snapshot.as_of < fold.cutoff for row in fold.train)
        assert all(row.label.observed_at <= fold.cutoff for row in fold.train)
        validated = {row.snapshot.match_id for row in fold.validate}
        assert not validated & {row.snapshot.match_id for row in fold.train}
    by_match = defaultdict(set)
    for row in rows:
        by_match[row.snapshot.match_id].add(row.snapshot.as_of)
    assert any(len(times) == 2 for times in by_match.values())
    with pytest.raises(ValueError, match="BLOCKED"):
        inner_folds(rows[:2], folds=3)


def test_schema_is_swap_closed_and_flags_missing_values(data):
    *_, rows, _ = data
    values = [dict(row.snapshot.values) for row in rows]
    schema = build_schema(
        values + [swap_values(item) for item in values],
        feature_set="core-v1",
        feature_set_sha256=rows[0].snapshot.feature_set_sha256,
        market_input=True,
    )
    numeric = set(schema.numeric)
    assert {"diff.elo", "p1.elo", "p2.elo", MARKET_FEATURE} <= numeric
    assert all(name.replace("p1.", "p2.", 1) in numeric for name in numeric if name[:3] == "p1.")
    assert MARKET_FEATURE in schema.flagged
    assert "match.tour" in schema.levels and "match.tour" not in numeric
    encoded = encode(values[0], schema)
    assert len(encoded) == len(schema.columns)
    assert math.isnan(encoded[schema.numeric.index(MARKET_FEATURE)])
    flag = len(schema.numeric) + schema.flagged.index(MARKET_FEATURE)
    assert encoded[flag] == 1.0


def test_tuning_records_every_trial_and_the_refit_is_reproducible(data):
    *_, rows, cutoff = data
    artifact = train_tabular(rows, training_cutoff=cutoff)
    assert len(artifact.trials) == len(DEFAULT_GRID) == 8
    assert {trial.params.monotone for trial in artifact.trials} == {False, True}
    best = min(artifact.trials, key=lambda trial: trial.log_loss)
    assert artifact.params == best.params
    assert all(len(trial.fold_log_loss) == 3 for trial in artifact.trials)
    assert artifact.library_version == "3.4.1"
    assert train_tabular(rows, training_cutoff=cutoff) == artifact

    booster = load_booster(artifact)
    schema = artifact.feature_schema
    sample = [dict(row.snapshot.values) for row in rows[:20]]
    forward = _symmetric(booster, schema, sample)
    reverse = _symmetric(booster, schema, [swap_values(item) for item in sample])
    assert all(abs(a + b - 1) < 1e-12 for a, b in zip(forward, reverse, strict=True))
    assert all(0 < p < 1 for p in forward)

    with pytest.raises(ValueError, match="do not match their hash"):
        load_booster(artifact.model_copy(update={"booster_sha256": "0" * 64}))


def test_monotone_trial_is_non_decreasing_in_the_elo_difference(data):
    *_, rows, cutoff = data
    config = TabularConfig(grid=DEFAULT_GRID[4:5], search_budget=1)
    artifact = train_tabular(rows, training_cutoff=cutoff, config=config)
    assert artifact.params.monotone
    booster = load_booster(artifact)
    base = dict(rows[0].snapshot.values)
    grid = [base | {"diff.elo": D(step)} for step in range(-400, 401, 50)]
    probabilities = _symmetric(booster, artifact.feature_schema, grid)
    assert all(b >= a - 1e-12 for a, b in zip(probabilities, probabilities[1:], strict=False))


def test_invalid_training_requests_are_blocked(data):
    *_, rows, cutoff = data
    with pytest.raises(ValueError, match="search budget"):
        train_tabular(rows, training_cutoff=cutoff, config=TabularConfig(search_budget=3))
    with pytest.raises(ValueError, match="BLOCKED"):
        train_tabular(rows[:30], training_cutoff=cutoff)
    with pytest.raises(ValueError, match="later than the training cutoff"):
        train_tabular(rows, training_cutoff=cutoff - timedelta(days=10))
    with pytest.raises(ValueError, match="BLOCKED"):
        train_tabular([], training_cutoff=cutoff)


def oof(index, p, **changes):
    values = {
        "match_id": f"00000000-0000-4000-8000-{index:012d}",
        "as_of": START + timedelta(days=index),
        "outcome": index % 2,
        "probabilities": (p, None),
        "fitted_at": (START, START),
        "in_sample": (False, False),
    }
    return OutOfFoldRow.model_validate(values | changes)


def test_stacker_rejects_in_sample_and_late_components_and_is_symmetric():
    rows = [oof(i, D("0.7") if i % 2 else D("0.35")) for i in range(1, 41)]
    cutoff = START + timedelta(days=60)
    artifact = fit_stacker(rows, components=("a", "b"), training_cutoff=cutoff, folds=3)
    assert artifact.weights[0] > 0 and artifact.weights[1] == 0
    assert artifact.missing_share == (D(0), D(1))
    assert stack(artifact, (D("0.8"), None)) + stack(artifact, (D("0.2"), None)) == 1
    leaked = rows[:-1] + [oof(40, D("0.7"), in_sample=(True, False))]
    with pytest.raises(StackingLeakage):
        fit_stacker(leaked, components=("a", "b"), training_cutoff=cutoff, folds=3)
    late = rows[:-1] + [oof(40, D("0.7"), fitted_at=(START + timedelta(days=41), START))]
    with pytest.raises(StackingLeakage):
        fit_stacker(late, components=("a", "b"), training_cutoff=cutoff, folds=3)
    with pytest.raises(StackingLeakage):
        fit_stacker(rows, components=("a", "b"), training_cutoff=START, folds=3)


def test_walk_forward_tabular_market_and_stack(data):
    state, snapshots, split, _, _ = data
    history, keys = quote_history(state)
    source = history_quotes(history, keys)

    def market(match_id, as_of):
        players = state.h.store.match(match_id).player_ids
        output = consensus(
            source(match_id, as_of), match_id=match_id, player_ids=players, as_of=as_of
        )
        return output.probability_player_one

    elo = BaselineCandidate(BaselineKind.SURFACE_ELO, bootstrap_draws=0)
    ranking = BaselineCandidate(BaselineKind.RANKING, bootstrap_draws=0)
    candidates = [
        elo,
        TabularCandidate(SMALL),
        TabularCandidate(
            SMALL.model_copy(update={"name": "tabular-xgb-small-market"}), market=market
        ),
        StackedCandidate((elo, ranking, TabularCandidate(SMALL))),
    ]
    run = run_walk_forward(
        state.h.store,
        snapshots,
        split,
        candidates,
        name="tabular-synthetic",
        tagger=state.tagger,
    )
    fits = {(item.model, item.fold): item.status for item in run.fits}
    assert all(status == "FITTED" for status in fits.values()), fits
    stacked = run.for_model("stacked-ensemble")
    assert stacked and all(item.support != SupportStatus.UNSUPPORTED for item in stacked)
    for name in ("tabular-xgb-small", "tabular-xgb-small-market", "stacked-ensemble"):
        pairs = matched(run.for_model(name), run.for_model("baseline-surface-elo"))
        assert len(pairs) >= 100, name
        paired_log_loss_difference(pairs, draws=50, seed=1, level=D("0.9"))
    with_market = run.for_model("tabular-xgb-small-market")
    without = run.for_model("tabular-xgb-small")
    assert len(matched(with_market, without)) == len(without)
