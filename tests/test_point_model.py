"""MOD-02: tennis format/state fixtures, solver coherence, simulation and point model."""

import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import tennis_oracle
from pit_support import history

from tennis_engine.models.baselines.contracts import SupportStatus
from tennis_engine.models.point.exact import (
    NonConvergence,
    hold,
    match_distribution,
    next_first,
    set_distribution,
    tiebreak,
    tiebreak_server,
)
from tennis_engine.models.point.formats import (
    BEST_OF_3_FINAL_TB10,
    BEST_OF_3_MATCH_TIEBREAK,
    BEST_OF_3_STANDARD,
    DecidingSet,
    MatchFormat,
    UnsupportedFormat,
    enabled,
)
from tennis_engine.models.point.model import PointModelConfig, fit, observations, predict
from tennis_engine.models.point.simulate import simulate

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "tennis" / "mod-02-cases.json").read_text()
)
TOL = FIXTURE["tolerance"]


def fmt_for(inputs):
    if inputs.get("match_tiebreak"):
        return BEST_OF_3_MATCH_TIEBREAK
    return BEST_OF_3_FINAL_TB10 if inputs.get("final_target") == 10 else BEST_OF_3_STANDARD


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_mod02_fixture(case):
    inputs, expected = case["inputs"], case["expected"]
    kind = case["kind"]
    if kind == "hold":
        assert hold(inputs["p"], advantage=inputs["advantage"]) == pytest.approx(expected, abs=TOL)
    elif kind == "tiebreak_server":
        assert tiebreak_server(inputs["index"], inputs["first"]) == expected
    elif kind == "tiebreak":
        actual = tiebreak(
            inputs["pa"], inputs["pb"], target=inputs["target"], first=inputs["first"]
        )
        assert actual == pytest.approx(expected, abs=TOL)
    elif kind == "next_server":
        assert next_first(inputs["first"], tuple(inputs["score"])) == expected
    elif kind == "set":
        actual = set_distribution(
            inputs["pa"],
            inputs["pb"],
            first=inputs["first"],
            fmt=BEST_OF_3_STANDARD,
            deciding=False,
        )
        assert {f"{a}-{b}": v for (a, b), v in actual.items()} == pytest.approx(expected, abs=TOL)
    elif kind == "match":
        actual = match_distribution(
            inputs["pa"], inputs["pb"], fmt_for(inputs), first=inputs["first"]
        ).correct_score
        assert {f"{a}-{b}": v for (a, b), v in actual.items()} == pytest.approx(expected, abs=TOL)
    else:  # pragma: no cover - fixture schema guard
        pytest.fail(f"unknown kind {kind}")


def test_fixture_set_is_large_enough_and_labelled():
    assert len(FIXTURE["cases"]) >= 40
    assert FIXTURE["independent_reviewer"] == "pending"


@pytest.mark.parametrize(
    "fmt", [BEST_OF_3_STANDARD, BEST_OF_3_FINAL_TB10, BEST_OF_3_MATCH_TIEBREAK]
)
def test_distribution_is_coherent_and_has_no_impossible_scores(fmt):
    dist = match_distribution(0.63, 0.6, fmt)
    assert dist.mass == pytest.approx(1, abs=1e-12)
    assert set(dist.correct_score) <= {(2, 0), (2, 1), (1, 2), (0, 2)}
    assert sum(dist.total_games.values()) == pytest.approx(1, abs=1e-12)
    assert sum(dist.game_margin.values()) == pytest.approx(1, abs=1e-12)
    assert dist.win_a == pytest.approx(dist.correct_score[(2, 0)] + dist.correct_score[(2, 1)])
    if fmt is BEST_OF_3_STANDARD:
        assert min(dist.total_games) == 12 and max(dist.total_games) == 39
    assert all(value >= 0 for value in dist.total_games.values())


def test_symmetry_and_player_swap_complement():
    assert match_distribution(0.62, 0.62, BEST_OF_3_STANDARD).win_a == pytest.approx(0.5, abs=1e-12)
    for first in (0, 1):
        forward = match_distribution(0.66, 0.59, BEST_OF_3_STANDARD, first=first).win_a
        swapped = match_distribution(0.59, 0.66, BEST_OF_3_STANDARD, first=1 - first).win_a
        assert forward + swapped == pytest.approx(1, abs=1e-12)


def test_endpoints_and_non_convergence():
    assert hold(1.0) == 1.0 and hold(0.0) == 0.0
    assert match_distribution(1.0, 0.0, BEST_OF_3_STANDARD).win_a == pytest.approx(1)
    with pytest.raises(NonConvergence):
        tiebreak(1.0, 1.0, target=7, first=0)
    with pytest.raises(ValueError):
        hold(1.2)


def test_only_verified_formats_are_enabled():
    assert enabled("bo3-tb7-v1") is BEST_OF_3_STANDARD
    with pytest.raises(UnsupportedFormat):
        enabled("bo5-final-tb10-v1")
    with pytest.raises(UnsupportedFormat):
        enabled("bo3-match-tb10-v1")
    advantage = MatchFormat(version="adv", best_of=3, deciding_set=DecidingSet.ADVANTAGE)
    with pytest.raises(UnsupportedFormat):
        match_distribution(0.6, 0.6, advantage)


def test_exact_and_simulation_agree_within_preregistered_bound():
    """Preregistered bound: |exact - simulated| <= 4 Monte Carlo standard errors."""
    exact = match_distribution(0.64, 0.61, BEST_OF_3_STANDARD).win_a
    result = simulate(0.64, 0.61, BEST_OF_3_STANDARD, matches=20000, seed=11)
    assert abs(result.win_a - exact) <= 4 * result.standard_error
    again = simulate(0.64, 0.61, BEST_OF_3_STANDARD, matches=20000, seed=11)
    assert again == result
    other = simulate(0.64, 0.61, BEST_OF_3_STANDARD, matches=20000, seed=12)
    assert other.win_a != result.win_a
    assert set(result.correct_score) <= {(2, 0), (2, 1), (1, 2), (0, 2)}
    oracle = tennis_oracle.match(0.64, 0.61, 0)
    solver = match_distribution(0.64, 0.61, BEST_OF_3_STANDARD, first=0).correct_score
    assert solver == pytest.approx(oracle, abs=1e-10)


# Point model on a synthetic history with consistent serve/return counts.

START = datetime(2026, 2, 2, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def stats_world():
    h = history()
    rng = random.Random(3)
    keys = [f"q{index}" for index in range(8)]
    for key in keys:
        h.player(key)
    serve = {key: rng.gauss(0, 0.25) for key in keys}
    ret = {key: rng.gauss(0, 0.25) for key in keys}
    matches = []
    for index in range(90):
        first, second = rng.sample(keys, 2)
        start = START + timedelta(hours=12 * index)
        counts = []
        for server, returner in ((first, second), (second, first)):
            p = 1 / (1 + math.exp(-(0.55 + serve[server] - ret[returner])))
            played = 70
            won = sum(rng.random() < p for _ in range(played))
            counts.append((played, won))
        stats = (
            {
                "serve_points": counts[0][0],
                "serve_points_won": counts[0][1],
                "return_points": counts[1][0],
                "return_points_won": counts[1][0] - counts[1][1],
            },
            {
                "serve_points": counts[1][0],
                "serve_points_won": counts[1][1],
                "return_points": counts[0][0],
                "return_points_won": counts[0][0] - counts[0][1],
            },
        )
        winner = first if counts[0][1] - counts[1][1] >= 0 else second
        h.match(f"pm-{index}", first, second, start=start, observed=start - timedelta(days=1))
        match_id = h.match(
            f"pm-{index}",
            first,
            second,
            start=start,
            observed=start + timedelta(hours=3),
            winner=winner,
            stats=stats,
        )
        matches.append((match_id, start))
    return h, matches, serve, ret


def test_point_model_fit_uses_only_known_stats_and_recovers_effects(stats_world):
    h, matches, serve, _ = stats_world
    cutoff = matches[70][1] - timedelta(hours=1)
    config = PointModelConfig(draws=20)
    artifact = fit(h.store, training_cutoff=cutoff, config=config)
    assert artifact.converged and artifact.observations == 2 * 70
    assert len(observations(h.store, cutoff, config)) == 140
    # The strongest true server has an estimated effect above the weakest one.
    best, worst = max(serve, key=serve.get), min(serve, key=serve.get)
    assert artifact.serve[h.pid(best)] > artifact.serve[h.pid(worst)]
    assert fit(h.store, training_cutoff=cutoff, config=config) == artifact
    with pytest.raises(ValueError, match="BLOCKED"):
        fit(h.store, training_cutoff=START - timedelta(days=30))


def test_point_model_predictions_support_rules_and_draws(stats_world):
    h, matches, _, _ = stats_world
    cutoff = matches[70][1] - timedelta(hours=1)
    artifact = fit(h.store, training_cutoff=cutoff, config=PointModelConfig(draws=20))
    match_id, start = matches[80]
    as_of = start - timedelta(hours=1)
    prediction = predict(h.store, match_id, artifact, as_of=as_of, fmt=BEST_OF_3_STANDARD)
    assert prediction.support == SupportStatus.SUPPORTED
    assert 0 < prediction.probability_player_one < 1
    assert prediction.draws == 20 and prediction.seed == artifact.config.seed
    assert prediction.draw_lower <= prediction.draw_upper
    assert prediction.draw_standard_error is not None
    again = predict(h.store, match_id, artifact, as_of=as_of, fmt=BEST_OF_3_STANDARD)
    assert again == prediction
    unknown = predict(h.store, match_id, artifact, as_of=as_of, fmt=None)
    assert unknown.support == SupportStatus.UNSUPPORTED
    assert unknown.reasons == ("format_unverified",)
    strict = artifact.model_copy(
        update={"config": artifact.config.model_copy(update={"min_weighted_points": 1e9})}
    )
    sparse = predict(h.store, match_id, strict, as_of=as_of, fmt=BEST_OF_3_STANDARD)
    assert "sparse_serve_return" in sparse.reasons and sparse.probability_player_one is None
    with pytest.raises(ValueError, match="Training data must end"):
        predict(
            h.store, match_id, artifact, as_of=cutoff - timedelta(days=1), fmt=BEST_OF_3_STANDARD
        )
