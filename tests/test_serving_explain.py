"""F14.5 explanation fidelity and SYS-12 read checks against the real F01 governance store."""

import re
from datetime import timedelta

import pytest
from conftest import source_policy
from serving_support import BOOK_SOURCE, FACTS, READ_AT, bet, build, headers, no_bet, stored, watch
from test_pricing_decision import fresh
from test_pricing_publication import observation

from tennis_engine.governance.contracts import (
    PayoutPolicy,
    PayoutSchedule,
    Principal,
    Role,
    SourcePolicy,
)
from tennis_engine.governance.service import GovernanceService
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.ingestion.bookmakers.quotes import Actionability
from tennis_engine.pricing.decision import Gate
from tennis_engine.serving.checks import GovernanceReadChecks, per_thread
from tennis_engine.serving.explain import GATE_TEXT, explain

NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")
BANNED = ("injur", "motivat", "guarantee", "certain", "sure ", "destined", "wants", "profit")


def allowed_numbers(item):
    record = item.record
    values = [record.decimal_odds, record.central_probability, record.conservative_probability]
    values.append(record.stake.amount)
    if record.value is not None:
        value = record.value
        values += [
            value.stake.amount,
            value.cash_return_if_win.amount,
            value.break_even_probability,
            value.expected_value,
            value.conservative_expected_value,
            value.conservative_roi,
        ]
    for fact in item.context.facts:
        values += [fact.selection_value, fact.opponent_value, fact.difference]
    found = {str(v) for v in values if v is not None}
    return found | {s.lstrip("+-") for s in found}


@pytest.mark.parametrize("make", [bet, watch, no_bet])
def test_every_number_in_an_explanation_is_a_stored_value(make):
    item = stored(make())
    statements = explain(item, lambda source_id: False)
    permitted = allowed_numbers(item)
    labels = [fact.label for fact in item.context.facts]
    for statement in statements:
        text = statement.text
        for label in labels:
            text = text.replace(label, "")
        for token in NUMBER.findall(text):
            assert token.lstrip("+-") in permitted, (token, statement.text)
        lowered = statement.text.lower()
        assert not any(word in lowered for word in BANNED), statement.text


def test_explanation_separates_observed_inferred_missing_and_decision():
    item = stored(watch())
    statements = explain(item, lambda source_id: False)
    kinds = {s.fact_key: s.kind for s in statements if s.fact_key}
    assert kinds == {"surface_elo": "INFERRED", "matches_90d": "OBSERVED", "minutes_48h": "MISSING"}
    elo = next(s for s in statements if s.fact_key == "surface_elo")
    assert "Model estimate." in elo.text and "Difference: +72.5 Elo points." in elo.text
    assert statements[0].kind == "OBSERVED" and "2.30" in statements[0].text
    decisions = [s.text for s in statements if s.kind == "DECISION"]
    assert decisions[-1] == "Decision: WATCH. No stake is recommended."
    assert GATE_TEXT[Gate.CONSERVATIVE_EV] in " ".join(decisions)


def test_bet_explanation_requires_manual_confirmation():
    statements = explain(stored(bet()), lambda source_id: False)
    closing = statements[-1].text
    assert closing.startswith("Decision: BET. Virtual stake")
    assert "Confirm the quote manually" in closing and "places no bets" in closing


def test_withheld_sources_hide_values_in_explanations():
    item = stored(bet())
    statements = explain(item, lambda source_id: True)
    assert {s.kind for s in statements if s.fact_key} == {"WITHHELD"}
    joined = set(NUMBER.findall(" ".join(s.text for s in statements)))
    for fact in FACTS:
        for value in (fact.selection_value, fact.opponent_value):
            if value is not None:
                assert str(value) not in joined


def test_every_gate_has_a_plain_sentence():
    assert set(GATE_TEXT) == set(Gate)


# Real governance store ---------------------------------------------------------------


@pytest.fixture
def governed(store, enabled, tmp_path, clock):
    book = source_policy(store, BOOK_SOURCE)
    revision = store.save(book, expected_revision=0, reason="Synthetic bookmaker source")
    quote_state = {"value": None}

    def actionability(item, now) -> Actionability | None:
        return quote_state["value"]

    reader = Principal(identity="fixture-dashboard", role=Role.DASHBOARD)
    factory = per_thread(
        lambda: GovernanceService(GovernanceStore(tmp_path / "governance.sqlite3", reader, clock))
    )
    checks = GovernanceReadChecks(factory, actionability)
    return store, book, revision, checks, quote_state


def test_governance_checks_allow_then_block_on_a_kill_switch(governed, store):
    _, book, revision, checks, quote_state = governed
    quote_state["value"] = fresh(observation=observation())
    payout = store.latest("payout", "synthetic-book", READ_AT)
    assert payout is not None
    record = bet()
    client, *_ = build([stored(record)], checks=checks)
    row = client.get("/v1/tennis/recommendations", headers=headers()).json()["recommendations"][0]
    assert row["actionable"] is True, row["read_time_reasons"]

    disabled = SourcePolicy.model_validate(
        book.model_dump() | {"version": "fixture-v2", "kill_switch": True}
    )
    store.save(disabled, expected_revision=revision, reason="Synthetic kill switch")
    row = client.get("/v1/tennis/recommendations", headers=headers()).json()["recommendations"][0]
    assert row["decision"] == "NO_BET" and row["actionable"] is False
    assert "SOURCE:synthetic-book:SOURCE_DISABLED" in row["read_time_reasons"]
    health = client.get("/v1/tennis/source-health", headers=headers()).json()
    assert {h["source_id"]: h["status"] for h in health}["synthetic-book"] == "DISABLED"


def test_global_disable_blocks_reads_and_shows_responsible_use_status(governed, store):
    _, _, _, checks, quote_state = governed
    quote_state["value"] = fresh(observation=observation())
    client, *_ = build([stored(bet())], checks=checks)
    store.set_global_disable(True, reason="Synthetic global stop")
    body = client.get("/v1/tennis/recommendations", headers=headers()).json()
    assert body["responsible_use"] == body["responsible_use"] | {
        "allowed": False,
        "reason": "GLOBAL_DISABLE",
    }
    row = body["recommendations"][0]
    assert row["actionable"] is False
    assert "RESPONSIBLE_USE:GLOBAL_DISABLE" in row["read_time_reasons"]


def test_missing_quote_state_blocks_a_bet(governed):
    _, _, _, checks, _ = governed
    client, *_ = build([stored(bet())], checks=checks)
    row = client.get("/v1/tennis/recommendations", headers=headers()).json()["recommendations"][0]
    assert row["actionable"] is False and "RECHECK:UNAVAILABLE" in row["read_time_reasons"]


def test_payout_policy_outside_its_interval_blocks_reads(governed, store):
    _, _, _, checks, quote_state = governed
    quote_state["value"] = fresh(observation=observation())
    row = store.latest("payout", "synthetic-book", READ_AT)
    policy = PayoutSchedule.model_validate_json(row["payload"]).policies[0]
    ended = PayoutPolicy.model_validate(
        policy.model_dump()
        | {"version": "fixture-v2", "effective_until": READ_AT - timedelta(seconds=1)}
    )
    store.save(
        PayoutSchedule(bookmaker="synthetic-book", policies=(ended,)),
        expected_revision=row["revision"],
        reason="Synthetic payout policy end",
    )
    client, *_ = build([stored(watch())], checks=checks)
    item = client.get("/v1/tennis/recommendations", headers=headers()).json()["recommendations"]
    assert item[0]["recorded_decision"] == "WATCH" and item[0]["decision"] == "NO_BET"
    assert "PAYOUT_POLICY:PAYOUT_POLICY_OUTSIDE_EFFECTIVE_INTERVAL" in item[0]["read_time_reasons"]
