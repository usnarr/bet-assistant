from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tennis_engine.ingestion.bookmakers.contracts import EventState, QuoteState, RawQuote
from tennis_engine.ingestion.bookmakers.quotes import (
    ActionabilityPolicy,
    ActionabilityReason,
    QuoteObservation,
    build_intervals,
    evaluate_actionability,
)

T0 = datetime(2026, 9, 21, 9, tzinfo=UTC)
START = T0 + timedelta(hours=2)
POLICY = ActionabilityPolicy()
SHA = "a" * 64


def quote(odds="1.85", state=QuoteState.OPEN, selection="s-1"):
    return RawQuote(
        bookmaker="synthetic-book",
        source_event_id="e-1",
        source_market_id="m-1",
        source_selection_id=selection,
        market_label="Match winner",
        market="TENNIS_MATCH_WINNER",
        selection_label="Player A",
        participant_index=0,
        line=None,
        decimal_odds=Decimal(odds),
        state=state,
    )


def seen(seconds, odds="1.85", state=QuoteState.OPEN, start=START, event=EventState.PRE_MATCH):
    return QuoteObservation(
        quote=quote(odds, state),
        observed_at=T0 + timedelta(seconds=seconds),
        parser_version="synthetic-book-v1",
        raw_content_sha256=SHA,
        scheduled_start=start,
        event_state=event,
    )


def check(observations, at_seconds, **kwargs):
    return evaluate_actionability(
        observations, at=T0 + timedelta(seconds=at_seconds), policy=POLICY, **kwargs
    )


def test_intervals_change_only_with_fingerprint_and_count_heartbeats():
    history = [seen(0), seen(30), seen(60, "1.90"), seen(90, "1.90"), seen(120, "1.85")]
    intervals = build_intervals(reversed(history))
    assert [item.quote.decimal_odds for item in intervals] == [
        Decimal("1.85"),
        Decimal("1.90"),
        Decimal("1.85"),
    ]
    assert intervals[0].heartbeats == 1 and intervals[0].valid_to == T0 + timedelta(seconds=60)
    assert intervals[1].observed_last_at == T0 + timedelta(seconds=90)
    assert intervals[2].valid_to is None
    suspended = build_intervals([seen(0), seen(10, state=QuoteState.SUSPENDED), seen(20)])
    assert [item.quote.state for item in suspended] == ["OPEN", "SUSPENDED", "OPEN"]


def test_two_spaced_observations_make_a_fresh_quote_actionable():
    result = check([seen(0), seen(25)], 40)
    assert result.actionable and result.confirmations == 2
    assert result.expires_at == T0 + timedelta(seconds=85)


@pytest.mark.parametrize(
    ("history", "at", "reason"),
    [
        ([], 10, ActionabilityReason.NO_OBSERVATION),
        ([seen(0)], 10, ActionabilityReason.QUOTE_UNCONFIRMED),
        ([seen(0), seen(10)], 15, ActionabilityReason.QUOTE_UNCONFIRMED),
        ([seen(0), seen(25, "1.90")], 30, ActionabilityReason.QUOTE_UNCONFIRMED),
        ([seen(0), seen(25)], 85, ActionabilityReason.QUOTE_STALE),
        (
            [seen(0), seen(25, state=QuoteState.SUSPENDED)],
            30,
            ActionabilityReason.QUOTE_SUSPENDED,
        ),
        ([seen(0), seen(25, state=QuoteState.CLOSED)], 30, ActionabilityReason.QUOTE_CLOSED),
        ([seen(0), seen(25, event=EventState.STARTED)], 30, ActionabilityReason.EVENT_STARTED),
        (
            [seen(0), seen(25, event=EventState.CANCELLED)],
            30,
            ActionabilityReason.EVENT_CANCELLED,
        ),
        ([seen(0, start=None), seen(25, start=None)], 30, ActionabilityReason.START_UNKNOWN),
        # The price did not move but the start changed, so the quote needs new confirmation.
        (
            [seen(0), seen(25), seen(50, start=START + timedelta(hours=1))],
            55,
            ActionabilityReason.QUOTE_UNCONFIRMED,
        ),
    ],
)
def test_non_actionable_quotes(history, at, reason):
    result = check(history, at)
    assert not result.actionable and reason in result.reasons
    assert result.expires_at is None


def test_scheduled_start_passing_blocks_the_quote():
    near = T0 + timedelta(seconds=40)
    result = check([seen(0, start=near), seen(25, start=near)], 45)
    assert ActionabilityReason.EVENT_STARTED in result.reasons


def test_expiry_never_outlives_the_scheduled_start():
    near = T0 + timedelta(seconds=50)
    result = check([seen(0, start=near), seen(25, start=near)], 30)
    assert result.actionable and result.expires_at == near


def test_later_observations_cannot_make_an_earlier_decision_fresh():
    history = [seen(0), seen(25), seen(200), seen(230)]
    # At 100 s only the first two observations exist; they are 75 s old by then.
    early = check(history, 100)
    assert ActionabilityReason.QUOTE_STALE in early.reasons
    assert early.observation is not None and early.observation.observed_at == T0 + timedelta(
        seconds=25
    )


def test_a_successful_poll_without_the_selection_withdraws_it():
    result = check([seen(0), seen(25)], 40, event_polls=[T0 + timedelta(seconds=35)])
    assert ActionabilityReason.QUOTE_WITHDRAWN in result.reasons
    # A poll after the decision time is not yet known and does not count.
    later = check([seen(0), seen(25)], 30, event_polls=[T0 + timedelta(seconds=35)])
    assert later.actionable


def test_mixed_keys_and_naive_times_are_rejected():
    other = seen(0).model_copy(update={"quote": quote(selection="s-2")})
    with pytest.raises(ValueError, match="one quote key"):
        check([seen(0), other], 10)
    with pytest.raises(ValueError):
        evaluate_actionability([seen(0)], at=datetime(2026, 9, 21), policy=POLICY)
    with pytest.raises(ValidationError):
        quote(odds="1.00")
    with pytest.raises(ValidationError):
        RawQuote.model_validate(quote().model_dump() | {"decimal_odds": 1.85})


@settings(max_examples=150, deadline=None)
@given(
    gaps=st.lists(st.integers(min_value=1, max_value=120), min_size=1, max_size=12),
    probe=st.integers(min_value=0, max_value=1500),
)
def test_property_actionable_quotes_are_always_fresh_confirmed_and_pre_start(gaps, probe):
    moments, total = [], 0
    for gap in gaps:
        total += gap
        moments.append(total)
    history = [seen(moment) for moment in moments]
    result = check(history, probe)
    if result.actionable:
        at = T0 + timedelta(seconds=probe)
        assert result.observation is not None
        assert at - result.observation.observed_at <= timedelta(seconds=60)
        assert result.observation.observed_at <= at < START
        assert result.confirmations >= 2
        assert result.expires_at is not None and result.expires_at > at
