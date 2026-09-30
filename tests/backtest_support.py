"""Seeded synthetic history for F13 harness tests (fictional players, synthetic data).

Every match has a schedule observed one day before the start, a result observed three
hours after the start, and serve/return counts drawn from the players' true serve and
return strengths. The winner is the player who won more points.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

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
