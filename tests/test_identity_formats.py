"""F04 deciding-set rules per edition, stage and best-of format, read as of a cutoff."""

from datetime import timedelta

import pytest
from identity_support import FIRST_OBSERVATION, world

from tennis_engine.contracts.domain import Availability
from tennis_engine.models.point.formats import (
    BEST_OF_3_FINAL_TB10,
    BEST_OF_3_STANDARD,
    UnsupportedFormat,
    for_rule,
    match_format,
)
from tennis_engine.normalization.contracts import (
    BestOf,
    DecidingSetRule,
    DrawStage,
    EditionFormatVersion,
    SourceFormatRecord,
)
from tennis_engine.normalization.formats import deciding_set_known_at
from tennis_engine.normalization.store import ReviewKind

RULE_TIME = FIRST_OBSERVATION + timedelta(hours=2)


def rule(deciding_set=DecidingSetRule.TIEBREAK_10, **overrides):
    return SourceFormatRecord.model_validate(
        {
            "source_id": "synthetic-sports",
            "source_tournament_id": "t-atp",
            "season": 2026,
            "draw_stage": DrawStage.MAIN,
            "best_of": BestOf.THREE,
            "deciding_set": deciding_set,
            "reference": "synthetic regulations section 3",
        }
        | overrides
    )


def seen(at):
    return Availability(observed_at=at, ingested_at=at)


def loaded(tmp_path):
    w = world(tmp_path)
    w.run()
    return w


def test_rule_applies_to_its_stage_from_its_observation_time(tmp_path):
    w = loaded(tmp_path)
    outcome = w.warehouse.ingest_format(rule(), seen(RULE_TIME))
    assert outcome.accepted and outcome.changed
    main = w.match_id("m-2")
    qualifying = w.match_id("m-1")
    assert deciding_set_known_at(w.store, main, RULE_TIME - timedelta(seconds=1)) is None
    known = deciding_set_known_at(w.store, main, RULE_TIME)
    assert known is not None and known.deciding_set == DecidingSetRule.TIEBREAK_10
    assert deciding_set_known_at(w.store, qualifying, RULE_TIME) is None
    assert match_format(w.store, main, RULE_TIME) == BEST_OF_3_FINAL_TB10
    with pytest.raises(UnsupportedFormat, match="unknown at the cutoff"):
        match_format(w.store, qualifying, RULE_TIME)


def test_repeat_is_idempotent_and_a_change_is_a_new_version(tmp_path):
    w = loaded(tmp_path)
    w.warehouse.ingest_format(rule(), seen(RULE_TIME))
    again = w.warehouse.ingest_format(rule(), seen(RULE_TIME + timedelta(hours=1)))
    assert again.accepted and not again.changed
    later = RULE_TIME + timedelta(days=1)
    changed = w.warehouse.ingest_format(
        rule(DecidingSetRule.TIEBREAK_7, reference="synthetic regulations, amended"),
        seen(later),
    )
    assert changed.changed
    main = w.match_id("m-2")
    history = w.store.edition_formats(w.store.match(main).edition_id, DrawStage.MAIN, BestOf.THREE)
    assert [item.version for item in history] == [1, 2]
    assert history[1].corrects_version == 1
    assert match_format(w.store, main, RULE_TIME) == BEST_OF_3_FINAL_TB10
    assert match_format(w.store, main, later) == BEST_OF_3_STANDARD


def test_unknown_rule_and_unverified_formats_abstain(tmp_path):
    w = loaded(tmp_path)
    main = w.match_id("m-2")
    w.warehouse.ingest_format(rule(DecidingSetRule.UNKNOWN), seen(RULE_TIME))
    assert deciding_set_known_at(w.store, main, RULE_TIME) is None
    later = RULE_TIME + timedelta(hours=1)
    w.warehouse.ingest_format(rule(DecidingSetRule.MATCH_TIEBREAK_10), seen(later))
    with pytest.raises(UnsupportedFormat, match="No verified format"):
        match_format(w.store, main, later)
    with pytest.raises(UnsupportedFormat):
        for_rule(3, DecidingSetRule.ADVANTAGE.value)
    with pytest.raises(UnsupportedFormat):
        for_rule(5, DecidingSetRule.TIEBREAK_10.value)


def test_unresolved_edition_or_unknown_scope_goes_to_review(tmp_path):
    w = loaded(tmp_path)
    missing = w.warehouse.ingest_format(rule(source_tournament_id="t-none"), seen(RULE_TIME))
    unknown = w.warehouse.ingest_format(rule(draw_stage=DrawStage.UNKNOWN), seen(RULE_TIME))
    for outcome in (missing, unknown):
        assert not outcome.accepted and outcome.review_id is not None
        assert w.store.review(outcome.review_id).kind == ReviewKind.RECORD


def test_contract_and_store_reject_invalid_versions(tmp_path):
    w = loaded(tmp_path)
    edition_id = w.store.match(w.match_id("m-2")).edition_id
    body = {
        "edition_id": edition_id,
        "draw_stage": DrawStage.MAIN,
        "best_of": BestOf.THREE,
        "version": 2,
        "deciding_set": DecidingSetRule.TIEBREAK_7,
        "reference": "ref",
        "source_id": "synthetic-sports",
        "availability": seen(RULE_TIME),
    }
    with pytest.raises(ValueError, match="Version must be 1"):
        w.store.append_edition_format(EditionFormatVersion.model_validate(body))
    with pytest.raises(ValueError, match="known draw stage"):
        EditionFormatVersion.model_validate(body | {"best_of": BestOf.UNKNOWN})
    with pytest.raises(ValueError, match="earlier version"):
        EditionFormatVersion.model_validate(body | {"corrects_version": 2})
