"""F13 harness: splits, walk-forward runner, metrics, bootstrap, economics and promotion.

All data is synthetic. Results here show that the harness works, not that a model works.
"""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from backtest_support import START, boundaries, world

from tennis_engine.backtesting.bootstrap import block_bootstrap
from tennis_engine.backtesting.bundle import FILES, write_bundle
from tennis_engine.backtesting.contracts import (
    EvaluationConfig,
    GateStatus,
    ScoredPrediction,
    combine,
    load_config,
)
from tennis_engine.backtesting.economics import ReplayBet, summarize
from tennis_engine.backtesting.metrics import (
    auc,
    forecast_metrics,
    matched,
    segment_report,
)
from tennis_engine.backtesting.promotion import PromotionGate, Review, decide_promotion
from tennis_engine.backtesting.runner import (
    BaselineCandidate,
    PointCandidate,
    run_walk_forward,
)
from tennis_engine.backtesting.splits import (
    RowAssignment,
    SplitError,
    validate_fit,
    validate_split,
    walk_forward,
)
from tennis_engine.backtesting.studies import ablation
from tennis_engine.features.contracts import AvailabilityMode
from tennis_engine.features.core import CORE_SET
from tennis_engine.features.dataset import build_dataset
from tennis_engine.features.environment import MemoryForecastStore, environment_set
from tennis_engine.models.baselines.baseline import TrainingRow
from tennis_engine.models.baselines.contracts import BaselineKind, SupportStatus
from tennis_engine.models.point.formats import BEST_OF_3_STANDARD
from tennis_engine.models.point.model import PointModelConfig

ROOT = Path(__file__).resolve().parents[1]
ELO = "baseline-surface-elo"
POINT = "point-serve-return"


def full_config(**changes) -> EvaluationConfig:
    """A complete synthetic study configuration; test settings, not an accepted policy."""
    values = {
        "version": "synthetic-study-v1",
        "reason": "Synthetic F13 harness test settings",
        "confidence_level": Decimal("0.9"),
        "bootstrap_draws": 200,
        "bootstrap_seed": 20261001,
        "min_test_rows": 50,
        "min_blocks": 4,
        "min_segment_rows": 20,
        "consensus_calibration_margin": Decimal("0.1"),
        "calibration_slope_bounds": (Decimal("0.5"), Decimal("2")),
        "calibration_intercept_limit": Decimal("0.5"),
        "min_winning_fold_fraction": Decimal("0.5"),
        "candidate_search_budget": 1,
        "minimum_roi_lower_bound": Decimal("0"),
        "frozen_at": START,
    }
    values.update(changes)
    return EvaluationConfig.model_validate(values)


def candidates(state):
    return [
        BaselineCandidate(BaselineKind.RANKING, bootstrap_draws=20),
        BaselineCandidate(BaselineKind.SURFACE_ELO, bootstrap_draws=20),
        PointCandidate(state.h.store, lambda _: BEST_OF_3_STANDARD, PointModelConfig(draws=10)),
    ]


@pytest.fixture(scope="module")
def study():
    state = world(count=150)
    manifest, snapshots = build_dataset(
        state.h.store,
        CORE_SET,
        state.rows(("24h", "1h")),
        name="mod-01-synthetic",
        mode=AvailabilityMode.PROSPECTIVE,
        cutoff_rule="first known start minus 24 hours and 1 hour",
        created_at=START,
        code_revision="0000000",
        source_versions={},
    )
    split = walk_forward(snapshots, boundaries(first_day=35, days=12), name="mod-01-synthetic")
    run = run_walk_forward(
        state.h.store,
        snapshots,
        split,
        candidates(state),
        name="mod-01-synthetic",
        dataset_id=manifest.dataset_id,
        tagger=state.tagger,
    )
    return state, manifest, snapshots, split, run


def scored(p, y, *, block="2026-w02", fold=0, **extra):
    at = START + timedelta(days=1)
    values = {
        "model": "m",
        "model_version": "v1",
        "artifact_sha256": None,
        "fold": fold,
        "training_cutoff": START,
        "match_id": uuid4(),
        "as_of": at,
        "snapshot_sha256": "0" * 64,
        "feature_set": "core-v1",
        "support": SupportStatus.SUPPORTED if p is not None else SupportStatus.UNSUPPORTED,
        "probability_player_one": None if p is None else Decimal(p),
        "outcome": y,
        "label_version": 1,
        "label_observed_at": at + timedelta(hours=4),
        "block": block,
        "tags": {},
    }
    values.update(extra)
    return ScoredPrediction.model_validate(values)


# Splits and leakage.


def test_cutoffs_of_one_match_share_a_fold_and_training_ends_at_the_cutoff(study):
    state, _, snapshots, split, run = study
    folds = {}
    for row in split.rows:
        folds.setdefault(row.match_id, set()).add(row.fold)
    assert all(len(item) == 1 for item in folds.values())
    assert split.excluded_after_last_fold > 0
    for prediction in run.predictions:
        fold = split.folds[prediction.fold]
        assert prediction.training_cutoff == fold.training_cutoff <= prediction.as_of
        assert (
            prediction.label_observed_at is None or prediction.label_observed_at > prediction.as_of
        )
    # A match whose 24-hour cutoff falls before a boundary keeps its 1-hour row in that fold.
    boundary = split.folds[1].test_start
    straddling = [row for row in split.rows if row.fold == 0 and row.as_of >= boundary]
    assert straddling, "fixture should contain a match that crosses a fold boundary"


def test_split_validation_rejects_a_match_in_two_folds(study):
    _, _, _, split, _ = study
    first = split.test_rows(0)[0]
    moved = RowAssignment(
        match_id=first.match_id,
        as_of=first.as_of + timedelta(hours=23),
        snapshot_sha256=first.snapshot_sha256,
        fold=1,
    )
    broken = split.model_copy(update={"rows": (*split.rows, moved)})
    with pytest.raises(SplitError, match="appears in folds"):
        validate_split(broken)


def test_fit_validation_rejects_test_period_rows_and_late_labels(study):
    state, _, snapshots, split, _ = study
    by_sha = {item.snapshot_sha256: item for item in snapshots}
    fold = split.folds[1]
    from tennis_engine.features.labels import final_label, label_known_at

    test_row = split.test_rows(1)[0]
    label = final_label(state.h.store, test_row.match_id)
    with pytest.raises(SplitError, match="tested fold"):
        validate_fit(split, fold, [TrainingRow(by_sha[test_row.snapshot_sha256], label)])
    # The latest fold-0 row known at the cutoff: its result arrives after the cutoff.
    earlier = [row for row in split.test_rows(0) if row.as_of <= fold.training_cutoff][-1]
    late = final_label(state.h.store, earlier.match_id)
    assert late is not None and late.observed_at > fold.training_cutoff
    with pytest.raises(SplitError, match="label was observed after"):
        validate_fit(split, fold, [TrainingRow(by_sha[earlier.snapshot_sha256], late)])
    known = label_known_at(state.h.store, earlier.match_id, fold.training_cutoff)
    assert known is None


# Runner (F13.1, MOD-01).


def test_runner_is_reproducible_and_records_every_fit(study):
    state, manifest, snapshots, split, run = study
    again = run_walk_forward(
        state.h.store,
        snapshots,
        split,
        candidates(state),
        name="mod-01-synthetic",
        dataset_id=manifest.dataset_id,
        tagger=state.tagger,
    )
    assert again.content_sha256 == run.content_sha256
    assert len(run.fits) == len(split.folds) * 3
    assert all(item.status == "FITTED" for item in run.fits)
    assert all(item.training_rows > 0 for item in run.fits)
    rows = sum(len(split.test_rows(item.index)) for item in split.folds)
    for model in run.models:
        assert len(run.for_model(model)) == rows


def test_blocked_fit_is_recorded_and_its_rows_are_unsupported(study):
    state = world(count=40, stats=False)
    _, snapshots = build_dataset(
        state.h.store,
        CORE_SET,
        state.rows(),
        name="no-stats",
        mode=AvailabilityMode.PROSPECTIVE,
        cutoff_rule="1h",
        created_at=START,
        code_revision="0000000",
        source_versions={},
    )
    split = walk_forward(snapshots, boundaries(1, first_day=12, days=8), name="no-stats")
    point = PointCandidate(state.h.store, lambda _: BEST_OF_3_STANDARD)
    run = run_walk_forward(state.h.store, snapshots, split, [point], name="no-stats")
    assert [item.status for item in run.fits] == ["BLOCKED"]
    assert "BLOCKED" in (run.fits[0].reason or "")
    assert run.predictions and all(item.reasons == ("fit_blocked",) for item in run.predictions)
    assert all(item.probability_player_one is None for item in run.predictions)


def test_unverified_format_abstains_and_lowers_coverage(study):
    state, _, snapshots, split, _ = study
    unverified = {row.match_id for row in split.test_rows(0)[:10]}
    point = PointCandidate(
        state.h.store,
        lambda match_id: None if match_id in unverified else BEST_OF_3_STANDARD,
        PointModelConfig(draws=0),
    )
    run = run_walk_forward(state.h.store, snapshots, split, [point], name="formats")
    report = {
        item.segment: item for item in segment_report(POINT, run.predictions, keys=(), min_rows=1)
    }
    assert report["all"].rows == len(run.predictions)
    skipped = sum(1 for item in run.predictions if item.match_id in unverified)
    assert skipped >= len(unverified)
    assert report["all"].supported == len(run.predictions) - skipped
    assert report["all"].coverage < 1
    assert all(
        item.reasons == ("format_unverified",)
        for item in run.predictions
        if item.match_id in unverified
    )


def test_segment_report_covers_folds_and_cutoffs_and_marks_sparse_segments(study):
    _, _, _, _, run = study
    report = {
        item.segment: item
        for item in segment_report(ELO, run.for_model(ELO), keys=("cutoff", "tour"), min_rows=40)
    }
    assert {"all", "fold=0", "fold=1", "fold=2", "cutoff=1h", "cutoff=24h", "tour=atp"} <= set(
        report
    )
    assert report["all"].status == GateStatus.PASS
    assert report["cutoff=1h"].scorable + report["cutoff=24h"].scorable == report["all"].scorable
    sparse = [item for item in report.values() if item.scorable < 40]
    assert all(item.status == GateStatus.INCONCLUSIVE for item in sparse)
    assert report["all"].log_loss is not None and report["all"].auc is not None


# Metrics and bootstrap (F13.6, F13.8).


def test_hand_calculated_forecast_metrics():
    rows = [scored("0.8", 1), scored("0.4", 0), scored(None, 1)]
    result = forecast_metrics("m", "all", rows, min_rows=2)
    # (-ln 0.8 - ln 0.6) / 2 and ((0.2)^2 + (0.4)^2) / 2
    assert result.log_loss == Decimal("0.366985")
    assert result.brier == Decimal("0.100000")
    assert result.accuracy == Decimal("1.000000")
    assert result.auc == Decimal("1.000000")
    assert (result.rows, result.supported, result.scorable) == (3, 2, 2)
    assert result.coverage == Decimal("0.666667")
    assert result.status == GateStatus.PASS
    assert forecast_metrics("m", "all", rows, min_rows=3).status == GateStatus.INCONCLUSIVE
    assert auc([Decimal("0.5"), Decimal("0.5")], [1, 0]) == Decimal("0.500000")
    assert auc([Decimal("0.5")], [1]) is None


def test_block_bootstrap_is_seeded_paired_and_needs_two_blocks():
    items = [
        (f"2026-w{week:02d}", Decimal(value), Decimal(1))
        for week, value in [(1, "-0.1"), (1, "-0.2"), (2, "0.05"), (3, "-0.3"), (4, "-0.05")]
    ]
    first = block_bootstrap(items, statistic="diff", draws=300, seed=5, level=Decimal("0.9"))
    again = block_bootstrap(items, statistic="diff", draws=300, seed=5, level=Decimal("0.9"))
    other = block_bootstrap(items, statistic="diff", draws=300, seed=6, level=Decimal("0.9"))
    assert first == again and first != other
    assert first.estimate == Decimal("-0.120000000")
    assert first.lower <= first.estimate <= first.upper
    assert (first.blocks, first.rows, first.usable_draws) == (4, 5, 300)
    single = block_bootstrap(items[:2], statistic="diff", draws=300, seed=5, level=Decimal("0.9"))
    assert single.lower is None and single.upper is None
    with pytest.raises(ValueError, match="undefined"):
        block_bootstrap(
            [("w", Decimal(1), Decimal(0))], statistic="roi", draws=1, seed=1, level=Decimal("0.9")
        )


def test_matched_rows_need_one_snapshot_and_label():
    a = scored("0.6", 1)
    b = a.model_copy(update={"model": "b", "probability_player_one": Decimal("0.5")})
    assert matched([a], [b]) == [(a, b)]
    with pytest.raises(ValueError, match="one snapshot"):
        matched([a], [b.model_copy(update={"outcome": 0})])
    unsupported = b.model_copy(
        update={"support": SupportStatus.UNSUPPORTED, "probability_player_one": None}
    )
    assert matched([a], [unsupported]) == []


def test_out_of_sample_contract_rejects_a_known_label():
    with pytest.raises(ValueError, match="out-of-sample"):
        scored("0.6", 1, label_observed_at=START + timedelta(days=1))


# Economics and CLV (F13.6, F13.7).


def test_economic_summary_hand_example_and_missing_clv():
    day = START + timedelta(days=1)

    def bet(index, stake, cash, closing=None):
        return ReplayBet(
            bet_id=uuid4(),
            block="2026-w02" if index < 3 else "2026-w03",
            decided_at=day,
            settled_at=day + timedelta(hours=index),
            stake=Decimal(stake),
            cash_return=Decimal(cash),
            odds=Decimal("2.50"),
            closing_odds=Decimal(closing) if closing else None,
            closing_policy="same-bookmaker-last-open-quote" if closing else None,
        )

    bets = [
        bet(0, "10.00", "25.00", closing="2.00"),
        bet(1, "10.00", "0.00"),
        bet(2, "10.00", "0.00"),
        bet(3, "10.00", "10.00"),
        bet(4, "10.00", "0.00"),
    ]
    summary = summarize(
        bets,
        decisions=["BET"] * 5 + ["NO_BET"] * 3 + ["WATCH"] * 2,
        actionable_decisions=8,
        starting_bankroll=Decimal("100.00"),
        execution_grade=False,
        assumptions=("synthetic acceptance of every stake",),
        draws=100,
        seed=1,
        level=Decimal("0.9"),
    )
    assert summary.profit == Decimal("-15.00")
    assert summary.roi == Decimal("-0.300000")
    # Equity 115, 105, 95, 95, 85: peak 115, drawdown 30.
    assert summary.max_drawdown == Decimal("30.00")
    assert summary.max_drawdown_fraction == Decimal("0.260870")
    # A void neither extends nor resets the losing streak.
    assert summary.longest_losing_streak == 3
    assert (summary.clv_observed, summary.clv_missing) == (1, 4)
    assert summary.mean_clv == Decimal("0.250000")
    assert summary.decisions == {"BET": 5, "WATCH": 2, "NO_BET": 3}
    assert summary.abstention_rate == Decimal("0.500000")
    assert summary.actionability_coverage == Decimal("0.800000")
    assert summary.roi_interval is not None and summary.roi_interval.blocks == 2
    with pytest.raises(ValueError, match="assumptions"):
        summarize(
            bets,
            decisions=["BET"],
            actionable_decisions=1,
            starting_bankroll=Decimal(100),
            execution_grade=False,
            assumptions=(),
            draws=1,
            seed=1,
            level=Decimal("0.9"),
        )
    with pytest.raises(ValueError, match="comparability"):
        ReplayBet(
            bet_id=uuid4(),
            block="w",
            decided_at=day,
            settled_at=None,
            stake=Decimal("1.00"),
            cash_return=None,
            odds=Decimal("2"),
            closing_odds=Decimal("1.9"),
        )


# Promotion decisions (F13.9).


def test_combine_never_turns_missing_evidence_into_pass():
    assert combine([]) == GateStatus.BLOCKED
    assert combine([GateStatus.PASS, GateStatus.INCONCLUSIVE]) == GateStatus.INCONCLUSIVE
    assert combine([GateStatus.BLOCKED, GateStatus.FAIL]) == GateStatus.FAIL


def test_repository_config_is_a_draft_that_blocks_promotion(study):
    *_, run = study
    config = load_config(ROOT / "configs" / "evaluations" / "release.json")
    assert config.missing()
    decision = decide_promotion(
        run,
        candidate=POINT,
        config=config,
        author="modeller",
        evaluated_at=START,
        decided_at=START,
    )
    assert decision.status == GateStatus.BLOCKED
    gates = {item.gate: item for item in decision.gates}
    assert gates[PromotionGate.CONFIG].status == GateStatus.BLOCKED
    assert gates[PromotionGate.BEATS_BASELINE].status == GateStatus.BLOCKED
    assert decision.comparison is None


def test_full_synthetic_study_stays_blocked_without_replay_consensus_and_review(study):
    *_, run = study
    decision = decide_promotion(
        run,
        candidate=POINT,
        config=full_config(),
        author="modeller",
        evaluated_at=START,
        decided_at=START,
        leakage_passed=True,
        rerun_sha256=run.content_sha256,
        rollback_target="baseline-surface-elo:v1",
    )
    gates = {item.gate: item for item in decision.gates}
    # The model result itself is synthetic; the decision must be the worst gate and never PASS.
    assert decision.status == combine(item.status for item in decision.gates)
    assert decision.status in (GateStatus.BLOCKED, GateStatus.FAIL)
    assert gates[PromotionGate.ECONOMICS].status == GateStatus.BLOCKED
    assert gates[PromotionGate.CONSENSUS_CALIBRATION].status == GateStatus.BLOCKED
    assert gates[PromotionGate.REVIEW].status == GateStatus.BLOCKED
    assert gates[PromotionGate.REPRODUCIBLE].status == GateStatus.PASS
    assert gates[PromotionGate.SAMPLE].status == GateStatus.PASS
    assert decision.comparison is not None and decision.comparison.blocks >= 4
    assert len(decision.gates) == len(PromotionGate)


def test_self_review_changed_config_and_failed_rerun_fail(study):
    *_, run = study
    decision = decide_promotion(
        run,
        candidate=POINT,
        config=full_config(frozen_at=START + timedelta(days=400)),
        author="modeller",
        evaluated_at=START + timedelta(days=200),
        decided_at=START + timedelta(days=200),
        leakage_passed=False,
        rerun_sha256="f" * 64,
        review=Review(reviewer="modeller", reviewed_at=START, approved=True),
    )
    gates = {item.gate: item for item in decision.gates}
    assert decision.status == GateStatus.FAIL
    for gate in (
        PromotionGate.CONFIG,
        PromotionGate.LEAKAGE,
        PromotionGate.REPRODUCIBLE,
        PromotionGate.REVIEW,
    ):
        assert gates[gate].status == GateStatus.FAIL, gate
    with pytest.raises(ValueError, match="own baseline"):
        decide_promotion(
            run,
            candidate=ELO,
            config=full_config(),
            author="a",
            evaluated_at=START,
            decided_at=START,
        )


# Bundle.


def test_bundle_is_immutable_hashed_and_holds_no_local_paths(study, tmp_path):
    *_, run = study
    config = full_config()
    metrics = segment_report(ELO, run.for_model(ELO), keys=("cutoff",), min_rows=20)
    decision = decide_promotion(
        run, candidate=POINT, config=config, author="modeller", evaluated_at=START, decided_at=START
    )
    target = write_bundle(
        tmp_path,
        run,
        config=config,
        metrics=metrics,
        decision=decision,
        provenance={"code_revision": "0000000"},
        limitations=("synthetic data only",),
    )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    import hashlib

    for name in FILES:
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == manifest["files"][name]
    for path in target.iterdir():
        text = path.read_text(encoding="utf-8")
        assert str(tmp_path) not in text and str(Path.home()) not in text
    lines = (target / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(run.predictions)
    with pytest.raises(FileExistsError):
        write_bundle(tmp_path, run, config=config, metrics=metrics, decision=None, provenance={})


# Studies: F10.7 and F08.8 on synthetic data.


def test_f10_7_point_model_and_surface_elo_share_matched_rows(study):
    *_, run = study
    pairs = matched(run.for_model(POINT), run.for_model(ELO))
    scorable = [item for item in run.for_model(POINT) if item.scorable]
    assert pairs and len(pairs) == len(scorable)
    assert all(a.snapshot_sha256 == b.snapshot_sha256 for a, b in pairs)


def test_f08_8_ablation_negative_control_for_environment_features(study):
    state, *_ = study
    rows = state.rows()[:80]
    runs, results = ablation(
        state.h.store,
        rows,
        [CORE_SET, environment_set(MemoryForecastStore())],
        BaselineCandidate(BaselineKind.SURFACE_ELO, bootstrap_draws=10),
        boundaries(2, first_day=20, days=10),
        name="f08-8-synthetic",
        created_at=START,
        code_revision="0000000",
        draws=50,
        seed=3,
        level=Decimal("0.9"),
    )
    (result,) = results
    assert result.feature_set == "core-env-v1" and result.matched_rows > 0
    # Surface Elo reads no environment feature, so the difference must be exactly zero.
    assert result.difference is not None and result.difference.estimate == 0
    env = runs["core-env-v1"].predictions
    assert {item.feature_set for item in env} == {"core-env-v1"}
