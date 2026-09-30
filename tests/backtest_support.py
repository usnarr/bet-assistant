"""Seeded synthetic history for F13 harness tests (fictional players, synthetic data).

Every match has a schedule observed one day before the start, a result observed three
hours after the start, and serve/return counts drawn from the players' true serve and
return strengths. The winner is the player who won more points.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pit_support import History, history

START = datetime(2026, 1, 5, 10, tzinfo=UTC)
PLAYERS = 12
CUTOFFS = {"24h": timedelta(hours=24), "1h": timedelta(hours=1)}


@dataclass
class World:
    h: History
    matches: list[tuple[object, datetime]] = field(default_factory=list)
    starts: dict[object, datetime] = field(default_factory=dict)

    def rows(self, offsets=("1h",)):
        """(match_id, cutoff) pairs; the 24-hour cutoff equals the schedule observation."""
        return [
            (match_id, start - CUTOFFS[offset] + timedelta(minutes=1 if offset == "24h" else 0))
            for match_id, start in self.matches
            for offset in offsets
        ]

    def tagger(self, snapshot):
        from tennis_engine.backtesting.runner import default_tags

        tags = default_tags(snapshot)
        hours = (self.starts[snapshot.match_id] - snapshot.as_of).total_seconds() / 3600
        tags["cutoff"] = "1h" if hours <= 1 else "24h"
        return tags


def world(seed: int = 11, count: int = 180, *, stats: bool = True) -> World:
    h = history()
    rng = random.Random(seed)
    keys = [f"w{index:02d}" for index in range(PLAYERS)]
    for key in keys:
        h.player(key)
    serve = {key: rng.gauss(0, 0.3) for key in keys}
    ret = {key: rng.gauss(0, 0.3) for key in keys}
    ranked = sorted(keys, key=lambda key: -(serve[key] + ret[key]))
    for position, key in enumerate(ranked, start=1):
        h.ranking(key, position * 9, dated=date(2026, 1, 1), observed=START - timedelta(days=3))
    state = World(h)
    for index in range(count):
        first, second = rng.sample(keys, 2)
        start = START + timedelta(hours=12 * index)
        counts = []
        for server, returner in ((first, second), (second, first)):
            p = 1 / (1 + math.exp(-(0.55 + serve[server] - ret[returner])))
            won = sum(rng.random() < p for _ in range(70))
            counts.append(won)
        records = (
            {
                "serve_points": 70,
                "serve_points_won": counts[0],
                "return_points": 70,
                "return_points_won": 70 - counts[1],
            },
            {
                "serve_points": 70,
                "serve_points_won": counts[1],
                "return_points": 70,
                "return_points_won": 70 - counts[0],
            },
        )
        winner = first if counts[0] >= counts[1] else second
        key = f"bt-{index:03d}"
        h.match(key, first, second, start=start, observed=start - timedelta(days=1))
        match_id = h.match(
            key,
            first,
            second,
            start=start,
            observed=start + timedelta(hours=3),
            winner=winner,
            stats=records if stats else (None, None),
        )
        state.matches.append((match_id, start))
        state.starts[match_id] = start
    return state


def boundaries(folds: int = 3, *, first_day: int = 40, days: int = 16) -> list[datetime]:
    return [START + timedelta(days=first_day + days * index) for index in range(folds + 1)]


BOOK = "synthetic-book"
QUOTE_OFFSETS = (timedelta(minutes=70), timedelta(minutes=40), timedelta(minutes=10))


def quote_history(state: World):
    """Synthetic F05 history: two-way prices at 70, 40 and 10 minutes before each start.

    The price moves at every observation, so a cutoff that sees a later price differs
    from one that does not. Nothing is observed before the 24-hour cutoff.
    """
    from tennis_engine.common.ids import stable_id
    from tennis_engine.ingestion.bookmakers.contracts import EventState, QuoteState, RawQuote
    from tennis_engine.ingestion.bookmakers.history import MemoryHistoryStore, QuoteHistory
    from tennis_engine.ingestion.bookmakers.quotes import QuoteObservation
    from tennis_engine.normalization.contracts import (
        MatchResolution,
        PlayerResolution,
        ResolutionAction,
        ResolutionDecision,
    )

    store = MemoryHistoryStore()
    keys = {}
    for index, (match_id, start) in enumerate(state.matches):
        players = state.h.store.match(match_id).player_ids
        event = f"ev-{index:03d}"
        mapped = start - timedelta(hours=2)
        participants = tuple(
            PlayerResolution(
                source_id=BOOK,
                source_player_id=str(player_id),
                source_name="name",
                decision=ResolutionDecision.AUTO_ACCEPT,
                action=ResolutionAction.LINK_EXISTING,
                player_id=player_id,
                candidates=(),
                reasons=(),
                policy_version="fixture-policy",
                resolved_at=mapped,
            )
            for player_id in players
        )
        store.add_mapping(
            stable_id("test-mapping", event),
            BOOK,
            MatchResolution(
                source_id=BOOK,
                source_event_id=event,
                decision=ResolutionDecision.AUTO_ACCEPT,
                action=ResolutionAction.LINK_EXISTING,
                match_id=match_id,
                swapped=False,
                participants=(participants[0], participants[1]),
                candidates=(),
                reasons=(),
                policy_version="fixture-policy",
                resolved_at=mapped,
            ),
        )
        keys[match_id] = [(BOOK, event, "mw", f"s{side}") for side in (0, 1)]
        for step, offset in enumerate(QUOTE_OFFSETS):
            p = 0.5 + 0.3 * math.sin(index + step)
            at = start - offset
            sha = f"{index:04d}{step:02d}".ljust(64, "0")
            for side, probability in ((0, p), (1, 1 - p)):
                odds = Decimal(str(round(1 / (probability * 1.05), 2)))
                store.add_observation(
                    stable_id("test-quote", f"{event}:{side}:{step}"),
                    QuoteObservation(
                        quote=RawQuote(
                            bookmaker=BOOK,
                            source_event_id=event,
                            source_market_id="mw",
                            source_selection_id=f"s{side}",
                            market_label="Winner",
                            market="TENNIS_MATCH_WINNER",
                            selection_label=f"Player {side}",
                            participant_index=side,
                            line=None,
                            decimal_odds=max(odds, Decimal("1.01")),
                            state=QuoteState.OPEN,
                        ),
                        observed_at=at,
                        parser_version="synthetic-book-v1",
                        raw_content_sha256=sha,
                        scheduled_start=start,
                        event_state=EventState.PRE_MATCH,
                    ),
                )
    return QuoteHistory(store), (lambda match_id: keys.get(match_id, []))
