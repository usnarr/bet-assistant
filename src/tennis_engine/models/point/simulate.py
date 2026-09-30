"""Point-by-point Monte Carlo simulator (F10.5, F10.6).

It plays every point with an explicit server, so it is an independent check of the exact
solver. Results carry the seed, the number of matches and the Monte Carlo standard error.
"""

import random
from dataclasses import dataclass
from math import sqrt

from .exact import tiebreak_server
from .formats import DecidingSet, MatchFormat, UnsupportedFormat


@dataclass(frozen=True)
class SimulationResult:
    seed: int
    matches: int
    win_a: float
    standard_error: float
    correct_score: dict[tuple[int, int], float]
    total_games: dict[int, float]


def _point(rng: random.Random, server: int, pa: float, pb: float) -> int:
    """Winner (0 or 1) of one point."""
    if server == 0:
        return 0 if rng.random() < pa else 1
    return 1 if rng.random() < pb else 0


def _game(rng: random.Random, server: int, pa: float, pb: float, advantage: bool) -> int:
    points = [0, 0]
    while True:
        points[_point(rng, server, pa, pb)] += 1
        high, low = max(points), min(points)
        if not advantage and points == [3, 3]:
            return _point(rng, server, pa, pb)
        if high >= 4 and high - low >= 2:
            return 0 if points[0] > points[1] else 1


def _tiebreak(rng: random.Random, first: int, pa: float, pb: float, target: int) -> int:
    points = [0, 0]
    index = 0
    while True:
        points[_point(rng, tiebreak_server(index, first), pa, pb)] += 1
        index += 1
        high, low = max(points), min(points)
        if high >= target and high - low >= 2:
            return 0 if points[0] > points[1] else 1


def _set(
    rng: random.Random, first: int, pa: float, pb: float, fmt: MatchFormat, deciding: bool
) -> tuple[int, int]:
    rule = fmt.deciding_set if deciding else DecidingSet.TIEBREAK_7
    if rule == DecidingSet.ADVANTAGE:
        raise UnsupportedFormat("Advantage final sets are not supported")
    if rule == DecidingSet.MATCH_TIEBREAK_10:
        return (1, 0) if _tiebreak(rng, first, pa, pb, 10) == 0 else (0, 1)
    target = 10 if rule == DecidingSet.TIEBREAK_10 else fmt.tiebreak_target
    games = [0, 0]
    while True:
        if games == [fmt.set_games, fmt.set_games]:
            winner = _tiebreak(rng, first, pa, pb, target)
            games[winner] += 1
            return games[0], games[1]
        server = first if sum(games) % 2 == 0 else 1 - first
        games[_game(rng, server, pa, pb, fmt.advantage_games)] += 1
        high, low = max(games), min(games)
        if high >= fmt.set_games and high - low >= 2:
            return games[0], games[1]


def simulate(
    pa: float,
    pb: float,
    fmt: MatchFormat,
    *,
    matches: int,
    seed: int,
    first: int | None = None,
) -> SimulationResult:
    """``first=None`` draws the first server with equal probability for each match."""
    if matches < 1:
        raise ValueError("At least one simulated match is required")
    rng = random.Random(seed)
    wins = 0
    scores: dict[tuple[int, int], int] = {}
    totals: dict[int, int] = {}
    need = fmt.sets_to_win
    for _ in range(matches):
        server = first if first is not None else rng.randrange(2)
        sets = [0, 0]
        games = 0
        while max(sets) < need:
            deciding = sets[0] == sets[1] == need - 1
            score = _set(rng, server, pa, pb, fmt, deciding)
            sets[0 if score[0] > score[1] else 1] += 1
            games += sum(score)
            if sum(score) % 2 == 1:
                server = 1 - server
        wins += sets[0] == need
        key = (sets[0], sets[1])
        scores[key] = scores.get(key, 0) + 1
        totals[games] = totals.get(games, 0) + 1
    rate = wins / matches
    return SimulationResult(
        seed=seed,
        matches=matches,
        win_a=rate,
        standard_error=sqrt(rate * (1 - rate) / matches),
        correct_score={key: count / matches for key, count in scores.items()},
        total_games={key: count / matches for key, count in totals.items()},
    )
