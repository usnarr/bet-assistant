"""SYS-07 leakage matrix, negative controls and snapshot reproducibility (synthetic)."""

from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pytest
from pit_support import history
from pydantic import ValidationError

from tennis_engine.contracts.domain import AvailabilityClass
from tennis_engine.features.asof import AsOfView, forecast_usable
from tennis_engine.features.context import CORE_CONTEXT_SET
from tennis_engine.features.contracts import AvailabilityMode, DatasetManifest
from tennis_engine.features.dataset import build_dataset, pre_match_cutoffs, write_dataset
from tennis_engine.features.labels import final_label
from tennis_engine.features.snapshots import LeakageError, SnapshotStore, build_features
from tennis_engine.normalization.contracts import SourcePlayerRecord

D = datetime(2026, 6, 1, tzinfo=UTC)
CUTOFF = D + timedelta(days=10)
TARGET_START = CUTOFF + timedelta(hours=2)
REVISION = "0123456789abcdef"


@pytest.fixture
def state():
    h = history()
    h.match("past-1", "alpha", "bravo", start=D, observed=D + timedelta(hours=3), winner="alpha")
    h.match(
        "past-2",
        "alpha",
        "charlie",
        start=D + timedelta(days=5),
        observed=D + timedelta(days=5, hours=3),
        winner="charlie",
    )
    h.ranking("alpha", 20, dated=date(2026, 6, 8), observed=D + timedelta(days=7))
    h.ranking("bravo", 40, dated=date(2026, 6, 8), observed=D + timedelta(days=7))
    h.target = h.match(
        "target", "alpha", "bravo", start=TARGET_START, observed=D + timedelta(days=8)
    )
    return h


def snap(state, *, at=CUTOFF, mode=AvailabilityMode.PROSPECTIVE):
    return build_features(state.store, state.target, at, CORE_CONTEXT_SET, mode=mode)


def side(state, key):
    match = state.store.match(state.target)
    return "p1" if match.player_ids[0] == state.pid(key) else "p2"


def test_baseline_snapshot_uses_only_known_history(state):
    row = snap(state)
    alpha = side(state, "alpha")
    assert row.values[f"{alpha}.history_matches"] == 2
    assert row.values[f"{alpha}.rank"] == 20
    assert row.values["match.best_of"] == "BEST_OF_3"
    assert row.availability == AvailabilityClass.PROSPECTIVE
    assert not row.research_only
    assert {item.kind for item in row.inputs} >= {"result", "ranking", "schedule", "match"}
    assert all(item.observed_at <= CUTOFF for item in row.inputs)


def test_target_result_after_cutoff_does_not_change_the_snapshot(state):
    before = snap(state)
    state.match(
        "target",
        "alpha",
        "bravo",
        start=TARGET_START,
        observed=TARGET_START + timedelta(hours=3),
        winner="alpha",
    )
    assert snap(state).snapshot_sha256 == before.snapshot_sha256
    with pytest.raises(LeakageError):
        snap(state, at=TARGET_START + timedelta(hours=4))


def test_ranking_published_after_cutoff_with_earlier_date_is_rejected(state):
    before = snap(state)
    state.ranking("alpha", 3, dated=date(2026, 6, 10), observed=CUTOFF + timedelta(minutes=1))
    after = snap(state)
    assert after.snapshot_sha256 == before.snapshot_sha256
    assert after.values[f"{side(state, 'alpha')}.rank"] == 20


def test_negative_control_ranking_observed_before_cutoff_changes_the_feature(state):
    state.ranking("alpha", 3, dated=date(2026, 6, 10), observed=CUTOFF - timedelta(minutes=1))
    assert snap(state).values[f"{side(state, 'alpha')}.rank"] == 3


def test_negative_control_new_past_match_observed_before_cutoff_changes_history(state):
    before = snap(state)
    state.match(
        "past-3",
        "alpha",
        "delta",
        start=D + timedelta(days=8),
        observed=D + timedelta(days=8, hours=3),
        winner="alpha",
    )
    after = snap(state)
    assert after.snapshot_sha256 != before.snapshot_sha256
    assert after.values[f"{side(state, 'alpha')}.history_matches"] == 3


def test_future_rows_leave_earlier_snapshots_unchanged(state):
    before = snap(state)
    for index in range(5):
        start = CUTOFF + timedelta(days=index + 1)
        state.match(
            f"future-{index}",
            "alpha",
            "charlie",
            start=start,
            observed=start + timedelta(hours=3),
            winner="alpha",
        )
    state.ranking("bravo", 1, dated=date(2026, 6, 20), observed=CUTOFF + timedelta(days=9))
    assert snap(state) == before


def test_old_import_without_evidence_is_research_only(state):
    """A historical result observed today has an early effective time but no proof."""
    imported_today = CUTOFF + timedelta(days=100)
    state.match(
        "import-1",
        "bravo",
        "delta",
        start=D + timedelta(days=2),
        observed=imported_today,
        winner="bravo",
    )
    prospective = snap(state)
    assert prospective.values[f"{side(state, 'bravo')}.history_matches"] == 1
    research = snap(state, mode=AvailabilityMode.RESEARCH_ONLY)
    assert research.values[f"{side(state, 'bravo')}.history_matches"] == 2
    assert research.availability == AvailabilityClass.RESEARCH_ONLY
    assert research.research_only


def test_archived_evidence_is_accepted_only_in_archived_mode(state):
    imported_today = CUTOFF + timedelta(days=100)
    state.match(
        "import-2",
        "bravo",
        "delta",
        start=D + timedelta(days=2),
        observed=imported_today,
        winner="bravo",
        source_available_at=D + timedelta(days=2, hours=4),
        availability_evidence_id="archive-review-fixture-2",
    )
    key = f"{side(state, 'bravo')}.history_matches"
    assert snap(state).values[key] == 1
    archived = snap(state, mode=AvailabilityMode.ARCHIVED)
    assert archived.values[key] == 2
    assert archived.availability == AvailabilityClass.ARCHIVED


def test_result_correction_after_cutoff_keeps_the_known_version(state):
    view_before = AsOfView(state.store, CUTOFF, AvailabilityMode.PROSPECTIVE)
    past = state.store.match_alias("synthetic-sports", "past-1").match_id
    before = snap(state)
    state.match(
        "past-1",
        "alpha",
        "bravo",
        start=D,
        observed=CUTOFF + timedelta(days=1),
        winner="bravo",
    )
    assert snap(state).snapshot_sha256 == before.snapshot_sha256
    (known, _) = AsOfView(state.store, CUTOFF, AvailabilityMode.PROSPECTIVE).result(past)
    assert known.version == 1 and known.winner_id == state.pid("alpha")
    assert view_before.result(past)[0].version == 1
    label = final_label(state.store, past)
    assert label is not None and label.corrected and label.result_version == 2
    assert label.player_one_won == (state.store.match(past).player_ids[0] == state.pid("bravo"))


def test_reschedule_after_cutoff_uses_schedule_known_at_cutoff(state):
    state.match(
        "target",
        "alpha",
        "bravo",
        start=TARGET_START + timedelta(days=1),
        observed=CUTOFF + timedelta(minutes=30),
    )
    view = AsOfView(state.store, CUTOFF, AvailabilityMode.PROSPECTIVE)
    assert view.schedule(state.target)[0].scheduled_start == TARGET_START
    rows, excluded = pre_match_cutoffs(
        state.store, [state.target], offset=timedelta(hours=2), mode=AvailabilityMode.PROSPECTIVE
    )
    assert rows == [(state.target, CUTOFF)] and not excluded


def test_corrected_alias_after_cutoff_is_invisible_to_the_view(state):
    record = SourcePlayerRecord(
        source_id="synthetic-stats", source_player_id="x-1", full_name="Stats Name", tour="ATP"
    )
    outcome = state.warehouse.ingest_player(record)
    state.clock.instant = CUTOFF - timedelta(days=1)
    state.warehouse.approve_player(
        outcome.review_id,
        reviewer="fixture-reviewer",
        reason="fixture",
        player_id=state.pid("alpha"),
    )
    state.clock.instant = CUTOFF + timedelta(days=1)
    state.warehouse.remap_player_alias(
        "synthetic-stats", "x-1", state.pid("bravo"), reviewer="fixture-reviewer", reason="fix"
    )
    view = AsOfView(state.store, CUTOFF, AvailabilityMode.PROSPECTIVE)
    assert view.player_alias("synthetic-stats", "x-1").player_id == state.pid("alpha")


def test_forecast_rules_reject_realized_and_late_weather():
    start = TARGET_START
    assert forecast_usable(
        issued_at=CUTOFF - timedelta(hours=6),
        valid_from=start - timedelta(hours=1),
        valid_to=start + timedelta(hours=1),
        scheduled_start=start,
        as_of=CUTOFF,
        realized=False,
    )
    assert not forecast_usable(
        issued_at=CUTOFF + timedelta(minutes=1),
        valid_from=start - timedelta(hours=1),
        valid_to=start + timedelta(hours=1),
        scheduled_start=start,
        as_of=CUTOFF,
        realized=False,
    )
    assert not forecast_usable(
        issued_at=CUTOFF - timedelta(hours=6),
        valid_from=start - timedelta(hours=1),
        valid_to=start + timedelta(hours=1),
        scheduled_start=start,
        as_of=CUTOFF,
        realized=True,
    )


def test_snapshot_integrity_and_immutable_store(state):
    row = snap(state)
    with pytest.raises(ValidationError, match="hash"):
        row.model_validate(row.model_dump() | {"values": row.values | {"p1.rank": 999}})
    store = SnapshotStore()
    store.put(row)
    assert store.put(snap(state)) is row
    forged = row.model_copy(update={"snapshot_sha256": "0" * 64})
    with pytest.raises(ValueError, match="cannot be replaced"):
        store.put(forged)


def test_frozen_dataset_rebuild_is_identical_and_immutable(state, tmp_path):
    rows, excluded = pre_match_cutoffs(
        state.store,
        [item.match_id for item in state.store.matches()],
        offset=timedelta(hours=2),
        mode=AvailabilityMode.PROSPECTIVE,
    )
    # Past matches were first observed after their start, so they have no pre-match row.
    assert excluded == Counter({"schedule_not_known_at_cutoff": 2})
    assert rows == [(state.target, CUTOFF)]
    kwargs = {
        "name": "sys-07-fixture",
        "mode": AvailabilityMode.PROSPECTIVE,
        "cutoff_rule": "first known scheduled start minus 2h",
        "created_at": CUTOFF,
        "code_revision": REVISION,
        "source_versions": {"synthetic-sports": "synthetic-warehouse-v1"},
        "random_seed": 7,
        "excluded": excluded,
    }
    first, snapshots = build_dataset(state.store, CORE_CONTEXT_SET, rows, **kwargs)
    second, again = build_dataset(state.store, CORE_CONTEXT_SET, rows, **kwargs)
    assert first == second and snapshots == again
    assert first.excluded == {"schedule_not_known_at_cutoff": 2}
    assert first.rows == ((state.target, CUTOFF, snapshots[0].snapshot_sha256),)
    assert first.availability == AvailabilityClass.PROSPECTIVE and not first.research_only
    artifact = write_dataset(tmp_path, first, snapshots)
    assert write_dataset(tmp_path, first, snapshots) == artifact
    assert first.reproducibility.feature_config_sha256 == CORE_CONTEXT_SET.sha256


def test_prospective_dataset_cannot_hide_research_rows(state):
    manifest, _ = build_dataset(
        state.store,
        CORE_CONTEXT_SET,
        [(state.target, CUTOFF)],
        name="mixed",
        mode=AvailabilityMode.RESEARCH_ONLY,
        cutoff_rule="fixed",
        created_at=CUTOFF,
        code_revision=REVISION,
        source_versions={},
    )
    data = manifest.model_dump() | {"mode": AvailabilityMode.PROSPECTIVE}
    data["availability"] = AvailabilityClass.RESEARCH_ONLY
    data["research_only"] = True
    data["class_counts"] = {AvailabilityClass.RESEARCH_ONLY: 1}
    with pytest.raises(ValidationError, match="prospective dataset"):
        DatasetManifest.model_validate(data)
    data["research_only"] = False
    with pytest.raises(ValidationError, match="research_only"):
        DatasetManifest.model_validate(data)


def test_core_feature_set_ignores_future_rows_and_target_result(state):
    from tennis_engine.features.core import CORE_SET

    before = build_features(state.store, state.target, CUTOFF, CORE_SET)
    state.match(
        "target",
        "alpha",
        "bravo",
        start=TARGET_START,
        observed=TARGET_START + timedelta(hours=3),
        winner="bravo",
        stats=(
            {"serve_points": 60, "serve_points_won": 30},
            {"serve_points": 60, "serve_points_won": 50},
        ),
    )
    state.match(
        "after-cutoff",
        "alpha",
        "charlie",
        start=CUTOFF + timedelta(days=2),
        observed=CUTOFF + timedelta(days=2, hours=3),
        winner="charlie",
    )
    assert build_features(state.store, state.target, CUTOFF, CORE_SET) == before
