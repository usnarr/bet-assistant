"""SYS-09: de-vig normalization, pair validity and consensus orientation (synthetic quotes)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import Market
from tennis_engine.ingestion.bookmakers.contracts import CanonicalQuote, QuoteState
from tennis_engine.models.baselines.contracts import SupportStatus
from tennis_engine.models.baselines.market import ConsensusConfig, PairError, consensus, devig

AS_OF = datetime(2026, 9, 20, 12, tzinfo=UTC)
MATCH = stable_id("match", "sys-09")
P1, P2 = sorted((stable_id("player", "one"), stable_id("player", "two")), key=str)
PAIR = (P1, P2)


def quote(player, odds, *, book="book-a", at=AS_OF, state=QuoteState.OPEN, match=MATCH, key=""):
    return CanonicalQuote(
        quote_id=stable_id("quote", f"{book}:{player}:{at.isoformat()}:{odds}:{key}"),
        bookmaker=book,
        match_id=match,
        market=Market.MATCH_WINNER,
        selection_player_id=player,
        decimal_odds=Decimal(odds),
        state=state,
        source_event_id="e-1",
        source_market_id="m-1",
        source_selection_id=str(player)[:8],
        source_order_swapped=False,
        scheduled_start=AS_OF + timedelta(hours=2),
        promotion_marker=None,
        observed_at=at,
        parser_version="synthetic-v1",
        raw_content_sha256="a" * 64,
        resolution_policy_version="identity-policy-v1-candidate",
    )


def test_proportional_devig_normalizes_and_keeps_displayed_odds():
    pair = devig(quote(P1, "1.80"), quote(P2, "2.10"), PAIR, ConsensusConfig())
    assert pair.odds == (Decimal("1.80"), Decimal("2.10"))
    assert pair.probabilities[0] + pair.probabilities[1] == 1
    # (1/1.8) / (1/1.8 + 1/2.1) = 2.1 / 3.9 = 0.538461...
    assert pair.probabilities[0].quantize(Decimal("1e-6")) == Decimal("0.538462")
    assert pair.overround.quantize(Decimal("1e-6")) == Decimal("0.031746")
    reversed_order = devig(quote(P2, "2.10"), quote(P1, "1.80"), PAIR, ConsensusConfig())
    assert reversed_order.probabilities == pair.probabilities


@pytest.mark.parametrize(
    ("first", "second", "reason"),
    [
        (quote(P1, "1.80"), quote(P2, "2.10", book="book-b"), "cross_bookmaker_pair"),
        (quote(P1, "1.80"), quote(P1, "2.10", key="x"), "incomplete_pair"),
        (quote(P1, "1.00"), quote(P2, "2.10"), "invalid_odds"),
        (quote(P1, "1.80", state=QuoteState.SUSPENDED), quote(P2, "2.10"), "suspended_or_closed"),
        (
            quote(P1, "1.80"),
            quote(P2, "2.10", at=AS_OF - timedelta(minutes=5)),
            "pair_not_contemporaneous",
        ),
        (quote(P1, "1.80"), quote(P2, "2.10", match=stable_id("match", "x")), "mismatched_pair"),
    ],
)
def test_invalid_pairs_are_rejected(first, second, reason):
    with pytest.raises(PairError) as error:
        devig(first, second, PAIR, ConsensusConfig())
    assert error.value.reason == reason


def test_consensus_is_freshness_weighted_in_logit_space_and_ignores_future_quotes():
    quotes = [
        quote(P1, "1.80"),
        quote(P2, "2.10"),
        quote(P1, "1.70", book="book-b", at=AS_OF - timedelta(minutes=10)),
        quote(P2, "2.25", book="book-b", at=AS_OF - timedelta(minutes=10)),
        quote(P1, "3.00", book="book-a", at=AS_OF + timedelta(minutes=1)),
        quote(P2, "1.40", book="book-a", at=AS_OF + timedelta(minutes=1)),
    ]
    output = consensus(quotes, match_id=MATCH, player_ids=PAIR, as_of=AS_OF)
    assert output.support == SupportStatus.SUPPORTED
    assert [pair.bookmaker for pair in output.pairs] == ["book-a", "book-b"]
    assert output.weights == (Decimal("1.000000000"), Decimal("0.500000000"))
    low, high = sorted(pair.probabilities[0] for pair in output.pairs)
    assert low < output.probability_player_one < high
    mirror = consensus(quotes, match_id=MATCH, player_ids=(P2, P1), as_of=AS_OF)
    assert abs(mirror.probability_player_one + output.probability_player_one - 1) <= Decimal("2e-9")


def test_consensus_without_a_valid_pair_is_unsupported():
    stale = [
        quote(P1, "1.80", at=AS_OF - timedelta(hours=1)),
        quote(P2, "2.10", at=AS_OF - timedelta(hours=1)),
        quote(P1, "1.90", book="book-b"),
    ]
    output = consensus(stale, match_id=MATCH, player_ids=PAIR, as_of=AS_OF)
    assert output.support == SupportStatus.UNSUPPORTED
    assert output.probability_player_one is None
    assert output.rejected == {"incomplete_pair": 1, "stale_pair": 1}
    single = consensus(
        [quote(P1, "1.80"), quote(P2, "2.10")], match_id=MATCH, player_ids=PAIR, as_of=AS_OF
    )
    assert single.support == SupportStatus.SPARSE and single.reasons == ("single_bookmaker",)
