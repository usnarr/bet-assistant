import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tennis_engine.ingestion.bookmakers.contracts import Snapshot
from tennis_engine.ingestion.bookmakers.history import MemoryHistoryStore, QuoteHistory
from tennis_engine.ingestion.bookmakers.quotes import ActionabilityPolicy, ActionabilityReason
from tennis_engine.ingestion.bookmakers.registry import BETCLIC

FIXTURES = Path(__file__).parent / "fixtures" / "bookmakers" / "betclic"
POLL_1 = datetime(2026, 9, 21, 8, tzinfo=UTC)
KEY_01 = ("betclic", "ev-01", "ev-01-mw", "ev-01-s1")
KEY_04 = ("betclic", "ev-04", "ev-04-mw", "ev-04-s2")


def snapshot(poll, observed_at, *, drop_event=None):
    body = (FIXTURES / f"payload-{poll}.json").read_bytes()
    listing = BETCLIC.parser.parse_listing(body)
    if drop_event is not None:
        listing = listing.model_copy(
            update={"quotes": tuple(q for q in listing.quotes if q.source_event_id != drop_event)}
        )
    return Snapshot.observe(
        listing, observed_at=observed_at, raw_content_sha256=hashlib.sha256(body).hexdigest()
    )


@pytest.fixture
def history():
    return QuoteHistory(MemoryHistoryStore())


def test_recording_is_idempotent_and_derives_intervals(history):
    first = snapshot(1, POLL_1)
    added = history.record(first)
    assert added == len(first.quotes)
    assert history.record(first) == 0  # Retry: no second observation.
    history.record(snapshot(2, POLL_1 + timedelta(seconds=30)))
    history.record(snapshot(2, POLL_1 + timedelta(seconds=60)))
    intervals = history.intervals(KEY_04, until=POLL_1 + timedelta(minutes=5))
    assert [str(item.quote.decimal_odds) for item in intervals] == ["2.90", "2.95"]
    assert intervals[0].valid_to == POLL_1 + timedelta(seconds=30)
    assert intervals[1].heartbeats == 1
    # An earlier cutoff sees only the history known then.
    assert len(history.intervals(KEY_04, until=POLL_1 + timedelta(seconds=10))) == 1


def test_actionability_reads_only_history_known_at_the_decision(history):
    history.record(snapshot(1, POLL_1))
    history.record(snapshot(2, POLL_1 + timedelta(seconds=30)))
    policy = ActionabilityPolicy()
    assert history.actionability(
        KEY_01, at=POLL_1 + timedelta(seconds=35), policy=policy
    ).actionable
    early = history.actionability(KEY_01, at=POLL_1 + timedelta(seconds=10), policy=policy)
    assert ActionabilityReason.QUOTE_UNCONFIRMED in early.reasons


def test_a_later_poll_without_the_selection_withdraws_it(history):
    history.record(snapshot(1, POLL_1))
    history.record(snapshot(2, POLL_1 + timedelta(seconds=30)))
    history.record(snapshot(2, POLL_1 + timedelta(seconds=40), drop_event="ev-01"))
    result = history.actionability(
        KEY_01, at=POLL_1 + timedelta(seconds=45), policy=ActionabilityPolicy()
    )
    assert ActionabilityReason.QUOTE_WITHDRAWN in result.reasons


def test_reused_observation_ids_with_other_content_are_rejected(history):
    first = snapshot(1, POLL_1)
    history.record(first)
    changed = first.model_copy(
        update={"quotes": (first.quotes[0].model_copy(update={"state": "CLOSED"}),)}
    )
    with pytest.raises(ValueError, match="reused"):
        history.record(changed)
