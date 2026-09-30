"""Seeded synthetic tennis history for F09/F10 model tests (fictional players)."""

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from pit_support import History, history

from tennis_engine.features.contracts import FeatureSnapshot
from tennis_engine.features.core import CORE_SET
from tennis_engine.features.labels import label_known_at
from tennis_engine.features.snapshots import build_features
from tennis_engine.models.baselines.baseline import TrainingRow

START = datetime(2026, 1, 5, 10, tzinfo=UTC)
PLAYERS = 16


@dataclass
class Synthetic:
    h: History
    matches: list[tuple[object, datetime]] = field(default_factory=list)
    snapshots: dict[object, FeatureSnapshot] = field(default_factory=dict)


def synthetic(seed: int = 7, count: int = 140) -> Synthetic:
    h = history()
    rng = random.Random(seed)
    keys = [f"p{index:02d}" for index in range(PLAYERS)]
    for key in keys:
        if key not in ("alpha", "bravo", "charlie", "delta"):
            h.player(key)
    strength = {key: rng.gauss(0, 1) for key in keys}
    ranked = sorted(keys, key=lambda key: -strength[key])
    for position, key in enumerate(ranked, start=1):
        h.ranking(key, position * 7, dated=date(2026, 1, 1), observed=START - timedelta(days=3))
    state = Synthetic(h)
    for index in range(count):
        first, second = rng.sample(keys, 2)
        start = START + timedelta(hours=8 * index)
        p_first = 1 / (1 + math.exp(-(strength[first] - strength[second])))
        winner = first if rng.random() < p_first else second
        match_key = f"s-{index:03d}"
        # Schedule observed one day before the start; result observed after the end.
        h.match(match_key, first, second, start=start, observed=start - timedelta(days=1))
        match_id = h.match(
            match_key,
            first,
            second,
            start=start,
            observed=start + timedelta(hours=3),
            winner=winner,
        )
        state.matches.append((match_id, start))
    return state


def rows(
    state: Synthetic, *, until: datetime | None = None
) -> list[tuple[FeatureSnapshot, object]]:
    """Pre-match snapshots one hour before each start, with labels known at ``until``."""
    out = []
    for match_id, start in state.matches:
        snapshot = state.snapshots.get(match_id)
        if snapshot is None:
            snapshot = build_features(state.h.store, match_id, start - timedelta(hours=1), CORE_SET)
            state.snapshots[match_id] = snapshot
        label = label_known_at(state.h.store, match_id, until) if until else None
        out.append((snapshot, label))
    return out


def training_rows(state: Synthetic, cutoff: datetime) -> list[TrainingRow]:
    result = []
    for snapshot, label in rows(state, until=cutoff):
        if snapshot.as_of <= cutoff and label is not None:
            result.append(TrainingRow(snapshot, label))
    return result
