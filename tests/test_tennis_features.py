"""SYS-08 hand calculations, orientation, sparse histories and quality gates."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pit_support import history

from tennis_engine.features.asof import AsOfView
from tennis_engine.features.contracts import AvailabilityMode
from tennis_engine.features.core import CORE_SET, swap_values
from tennis_engine.features.quality import QualityStatus, assess
from tennis_engine.features.ratings import (
    EloConfig,
    FormConfig,
    PlayedMatch,
    RatingTable,
    expected_score,
    form,
    k_factor,
    replay,
    shrink_for_inactivity,
)
from tennis_engine.features.serve_return import shrink
from tennis_engine.features.snapshots import FeatureContext, build_features, fixed
from tennis_engine.normalization.contracts import Surface

D = datetime(2026, 3, 1, tzinfo=UTC)
FLAT = EloConfig(k_numerator=Decimal(32), k_shape=Decimal(0), k_offset=Decimal(1))
Q6 = Decimal("0.000001")


def q(value: Decimal) -> Decimal:
    return value.quantize(Q6)


def test_elo_hand_calculations():
    assert expected_score(Decimal(1500), Decimal(1500), Decimal(400)) == Decimal("0.5")
    table = RatingTable(FLAT)
    a, b = table_ids = ("a", "b")
    del table_ids
    from uuid import NAMESPACE_OID, uuid5

    pa, pb = uuid5(NAMESPACE_OID, a), uuid5(NAMESPACE_OID, b)
    expected_w, expected_l = table.update(pa, pb, D)
    assert (expected_w, expected_l) == (Decimal("0.5"), Decimal("0.5"))
    assert table.current(pa, D).rating == Decimal(1516)
    assert table.current(pb, D).rating == Decimal(1484)
    # 1 / (1 + 10 ** (-32 / 400)) = 0.545922...
    assert q(expected_score(Decimal(1516), Decimal(1484), Decimal(400))) == Decimal("0.545922")
    # Default K = 250 / (0 + 5) ** 0.4 = 131.326...
    assert q(k_factor(0, EloConfig())) == Decimal("131.326390")


def test_inactivity_shrinkage_hand_calculation():
    config = EloConfig()
    last = D
    assert shrink_for_inactivity(Decimal(1600), last, D + timedelta(days=90), config) == 1600
    # 60 idle days beyond 90: 1500 + 100 * 0.97 ** 2 = 1594.09
    shrunk = shrink_for_inactivity(Decimal(1600), last, D + timedelta(days=150), config)
    assert q(shrunk) == Decimal("1594.090000")
    assert shrink_for_inactivity(Decimal(1600), None, D, config) == 1600


def test_form_weights_and_effective_sample_size():
    config = FormConfig(half_life_days=Decimal(60), prior_weight=Decimal(2))
    from uuid import NAMESPACE_OID, uuid5

    other = uuid5(NAMESPACE_OID, "x")
    one = [PlayedMatch(D, other, True, Decimal("0.5"), Surface.HARD)]
    result = form(one, D + timedelta(days=60), config)
    # weight 0.5, residual 0.5: 0.25 / (0.5 + 2) = 0.1; ESS = 0.25 / 0.25 = 1
    assert q(result.value) == Decimal("0.100000")
    assert q(result.effective_sample_size) == Decimal("1.000000")
    two = one + [PlayedMatch(D + timedelta(days=60), other, False, Decimal("0.25"), Surface.HARD)]
    result = form(two, D + timedelta(days=60), config)
    # weights 0.5 and 1: ESS = 1.5**2 / 1.25 = 1.8; form = (0.25 - 0.25) / 3.5 = 0
    assert q(result.effective_sample_size) == Decimal("1.800000")
    assert result.value == 0
    assert form([], D, config).value is None
    old = [PlayedMatch(D, other, True, Decimal("0.5"), Surface.HARD)]
    assert form(old, D + timedelta(days=400), config).matches == 0


def test_beta_binomial_shrinkage_hand_calculation():
    assert shrink(10, 20, Decimal("0.6"), Decimal(20)) == Decimal("0.55")
    assert shrink(0, 0, Decimal("0.6"), Decimal(20)) == Decimal("0.6")
    assert shrink(0, 10, Decimal("0.6"), Decimal(20)) == Decimal("0.4")


@pytest.fixture
def state():
    h = history()
    stats = (
        {"serve_points": 80, "serve_points_won": 56, "return_points": 70, "return_points_won": 28},
        {"serve_points": 70, "serve_points_won": 42, "return_points": 80, "return_points_won": 24},
    )
    h.match(
        "m1",
        "alpha",
        "bravo",
        start=D,
        observed=D + timedelta(hours=3),
        winner="alpha",
        stats=stats,
    )
    h.match(
        "m2",
        "alpha",
        "charlie",
        start=D + timedelta(days=3),
        observed=D + timedelta(days=3, hours=3),
        winner="charlie",
    )
    h.match(
        "wo",
        "bravo",
        "charlie",
        start=D + timedelta(days=4),
        observed=D + timedelta(days=4, hours=3),
        winner="bravo",
        status="WALKOVER",
        sets=(),
    )
    h.match(
        "ret",
        "bravo",
        "delta",
        start=D + timedelta(days=5),
        observed=D + timedelta(days=5, hours=3),
        winner="delta",
        status="RETIRED",
        sets=({"games": (6, 4)}, {"games": (1, 2)}),
    )
    h.ranking("alpha", 10, dated=date(2026, 3, 2), observed=D + timedelta(days=1))
    h.target = h.match(
        "target", "alpha", "bravo", start=D + timedelta(days=10), observed=D + timedelta(days=6)
    )
    h.cutoff = D + timedelta(days=9)
    return h


def test_replay_skips_walkovers_and_retirements_by_default(state):
    view = AsOfView(state.store, state.cutoff, AvailabilityMode.PROSPECTIVE)
    rated = replay(view, EloConfig(), exclude=state.target)
    assert rated.skipped == {"walkover": 1, "retirement": 1}
    assert len(rated.rated) == 2
    included = replay(view, EloConfig(retirements="INCLUDE"), exclude=state.target)
    assert included.skipped == {"walkover": 1}


def test_core_snapshot_values_lineage_and_missingness(state):
    row = build_features(state.store, state.target, state.cutoff, CORE_SET)
    match = state.store.match(state.target)
    alpha = "p1" if match.player_ids[0] == state.pid("alpha") else "p2"
    bravo = "p2" if alpha == "p1" else "p1"
    assert row.values[f"{alpha}.elo_matches"] == 2
    assert row.values[f"{bravo}.elo_matches"] == 1
    assert row.values[f"{alpha}.serve_points"] == 80
    assert row.values[f"{alpha}.serve_rate_raw"] == fixed(Decimal("0.7"))
    # (56 + 0.64 * 200) / (80 + 200) = 0.657143
    assert row.values[f"{alpha}.serve_rate"] == Decimal("0.657143")
    assert row.values[f"{alpha}.stats_coverage"] == Decimal("0.500000")
    assert row.values[f"{alpha}.stats_missing"] is False
    assert row.values[f"{alpha}.sets_28d"] == 4
    assert row.values[f"{alpha}.minutes_28d"] == 240
    assert row.values[f"{alpha}.duration_incomplete_28d"] is False
    assert row.values[f"{alpha}.matches_3d"] == 0
    assert row.values[f"{bravo}.rank"] is None and f"{bravo}.rank" in row.missing
    assert row.values["diff.log_rank"] is None
    assert row.values["diff.elo"] == row.values["p1.elo"] - row.values["p2.elo"]
    assert all(value is None or value == value for value in row.values.values())
    assert any(item.kind == "stats" for item in row.inputs)
    assert all(item.observed_at <= state.cutoff for item in row.inputs)


def test_new_player_gets_declared_priors_and_flags(state):
    state.player("echo")
    target = state.match(
        "new", "echo", "delta", start=D + timedelta(days=12), observed=D + timedelta(days=6)
    )
    row = build_features(state.store, target, state.cutoff, CORE_SET)
    match = state.store.match(target)
    echo = "p1" if match.player_ids[0] == state.pid("echo") else "p2"
    assert row.values[f"{echo}.elo"] == fixed(Decimal(1500))
    assert row.values[f"{echo}.elo_matches"] == 0
    assert row.values[f"{echo}.form"] is None
    assert row.values[f"{echo}.form_ess"] == 0
    assert row.values[f"{echo}.stats_missing"] is True
    assert row.values[f"{echo}.serve_rate"] == fixed(Decimal("0.64"))
    assert row.values[f"{echo}.serve_rate_raw"] is None
    assert row.values[f"{echo}.stats_coverage"] is None
    assert row.values[f"{echo}.days_since_last_match"] is None


def test_unknown_surface_yields_no_surface_rating(state):
    state.tournament("u", surface="UNKNOWN")
    target = state.match(
        "unk",
        "alpha",
        "bravo",
        start=D + timedelta(days=12),
        observed=D + timedelta(days=6),
        tournament="u",
    )
    row = build_features(state.store, target, state.cutoff, CORE_SET)
    assert row.values["p1.surface_elo"] is None and row.values["diff.surface_elo"] is None
    assert row.values["match.surface"] == "UNKNOWN"


def test_player_swap_negates_directional_features(state):
    row = build_features(state.store, state.target, state.cutoff, CORE_SET)
    view = AsOfView(state.store, state.cutoff, AvailabilityMode.PROSPECTIVE)
    match = state.store.match(state.target)
    players = (state.store.player(match.player_ids[1]), state.store.player(match.player_ids[0]))
    context = FeatureContext(view, match, state.store.edition(match.edition_id), players)
    reversed_values = {}
    for _, group in CORE_SET.groups:
        reversed_values.update(group(context))
    assert swap_values(reversed_values) == row.values


def test_quality_hard_gates_cannot_be_averaged_away(state):
    row = build_features(state.store, state.target, state.cutoff, CORE_SET)
    blocked = assess(row, identity_resolved=None)
    assert not blocked.usable
    assert set(blocked.hard_failures) == {"identity_unknown", "odds_unknown", "rules_unknown"}
    ready = assess(
        row,
        identity_resolved=True,
        odds_ready=True,
        rules_ready=True,
        minimum_stats_coverage=Decimal(0),
    )
    # One of two rated matches per player has stats (the walkover does not count).
    assert ready.usable and ready.soft_score == Decimal("0.5")
    wrong = assess(
        row,
        identity_resolved=False,
        odds_ready=True,
        rules_ready=True,
        minimum_stats_coverage=Decimal(0),
    )
    assert not wrong.usable and wrong.hard_failures == ("identity_failed",)
    stats = next(item for item in wrong.components if item.component == "STATS")
    assert stats.status == QualityStatus.PASS and not stats.hard


def test_unknown_format_fails_format_gate(state):
    target = state.match(
        "fmt",
        "alpha",
        "charlie",
        start=D + timedelta(days=12),
        observed=D + timedelta(days=6),
        best_of="UNKNOWN",
    )
    row = build_features(state.store, target, state.cutoff, CORE_SET)
    report = assess(row, identity_resolved=True, odds_ready=True, rules_ready=True)
    assert "unsupported_or_unknown_format" in report.hard_failures


def test_feature_dictionary_is_complete_and_unique():
    names = [item.name for item in CORE_SET.definitions]
    assert len(names) == len(set(names))
    assert all(item.unit and item.missing_behavior for item in CORE_SET.definitions)
    assert len(CORE_SET.sha256) == 64
