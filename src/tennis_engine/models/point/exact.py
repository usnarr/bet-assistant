"""Exact match distributions from service-point probabilities (F10.2–F10.4, F10.6).

Players are A (index 0) and B (index 1). ``pa`` is the probability that A wins a point on
A's serve; ``pb`` is the same for B. Points are independent with fixed probabilities.
Deuce and tied tiebreak states use closed forms, so no truncation is involved.

Service order:

- Games alternate servers. The first server of a set serves games 1, 3, 5, ...
- In a tiebreak, the player whose turn it is serves point 1, then servers change every
  two points (A, B, B, A, A, ...).
- The next set starts with the player who did not serve first in the previous set if
  that set had an odd number of games (a tiebreak counts as one game), else the same.
"""

from collections import defaultdict
from dataclasses import dataclass
from functools import cache, lru_cache
from math import comb

from .formats import DecidingSet, MatchFormat, UnsupportedFormat

TOLERANCE = 1e-12


class NonConvergence(ValueError):
    """A tied state can never be resolved, for example when both servers never lose."""


def _check(p: float) -> float:
    if not 0.0 <= p <= 1.0:
        raise ValueError("A point probability must be in [0, 1]")
    return p


def hold(p: float, *, advantage: bool = True) -> float:
    """Probability that the server wins a game (closed form)."""
    _check(p)
    q = 1.0 - p
    before_deuce = sum(comb(3 + k, k) * p**4 * q**k for k in range(3))
    at_deuce = comb(6, 3) * p**3 * q**3
    if advantage:
        denominator = 1.0 - 2.0 * p * q
        return before_deuce + at_deuce * (p * p / denominator)
    return before_deuce + at_deuce * p


def tiebreak_server(point_index: int, first: int) -> int:
    """Server (0 or 1) of the zero-based tiebreak point ``point_index``."""
    return first if ((point_index + 1) // 2) % 2 == 0 else 1 - first


def _tied(win_on_own: tuple[float, float]) -> float:
    """P(A wins from a tie resolved by pairs of units, one won on each side's serve)."""
    a, b = win_on_own
    both = a * (1.0 - b)
    lose = (1.0 - a) * b
    if both + lose == 0.0:
        raise NonConvergence("The tied state never resolves")
    return both / (both + lose)


def tiebreak(pa: float, pb: float, *, target: int, first: int) -> float:
    """P(A wins a first-to-``target``, win-by-two tiebreak) with exact service order."""
    _check(pa), _check(pb)

    @cache
    def win(i: int, j: int) -> float:
        if i >= target and i - j >= 2:
            return 1.0
        if j >= target and j - i >= 2:
            return 0.0
        if i == j and i >= target - 1:
            return _tied((pa, pb))
        server = tiebreak_server(i + j, first)
        point = pa if server == 0 else 1.0 - pb
        return point * win(i + 1, j) + (1.0 - point) * win(i, j + 1)

    return win(0, 0)


SetScore = tuple[int, int]


def set_distribution(
    pa: float, pb: float, *, first: int, fmt: MatchFormat, deciding: bool
) -> dict[SetScore, float]:
    """Distribution of final set scores in games. A tiebreak set ends 7-6 or 6-7."""
    rule = fmt.deciding_set if deciding else DecidingSet.TIEBREAK_7
    if rule == DecidingSet.ADVANTAGE:
        raise UnsupportedFormat("Advantage final sets are not supported; abstain")
    if rule == DecidingSet.MATCH_TIEBREAK_10:
        a = tiebreak(pa, pb, target=10, first=first)
        return {(1, 0): a, (0, 1): 1.0 - a}
    target = 10 if rule == DecidingSet.TIEBREAK_10 else fmt.tiebreak_target
    games = fmt.set_games
    holds = (hold(pa, advantage=fmt.advantage_games), hold(pb, advantage=fmt.advantage_games))
    result: dict[SetScore, float] = defaultdict(float)
    states: dict[SetScore, float] = {(0, 0): 1.0}
    while states:
        following: dict[SetScore, float] = defaultdict(float)
        for (a, b), mass in states.items():
            if a == games and b == games:
                won = tiebreak(pa, pb, target=target, first=first)
                result[(games + 1, games)] += mass * won
                result[(games, games + 1)] += mass * (1.0 - won)
                continue
            server = first if (a + b) % 2 == 0 else 1 - first
            a_wins = holds[0] if server == 0 else 1.0 - holds[1]
            for score, weight in (((a + 1, b), a_wins), ((a, b + 1), 1.0 - a_wins)):
                x, y = score
                if (x >= games and x - y >= 2) or (y >= games and y - x >= 2) or max(x, y) > games:
                    result[score] += mass * weight
                else:
                    following[score] += mass * weight
        states = following
    return dict(result)


def next_first(first: int, score: SetScore) -> int:
    return 1 - first if sum(score) % 2 == 1 else first


@dataclass(frozen=True)
class MatchDistribution:
    format_version: str
    win_a: float
    correct_score: dict[tuple[int, int], float]
    total_games: dict[int, float]
    game_margin: dict[int, float]

    @property
    def mass(self) -> float:
        return sum(self.correct_score.values())


def _match(pa: float, pb: float, first: int, fmt: MatchFormat) -> MatchDistribution:
    need = fmt.sets_to_win
    states: dict[tuple[int, int, int, int, int], float] = {(0, 0, first, 0, 0): 1.0}
    finished: dict[tuple[int, int, int, int], float] = defaultdict(float)
    cache: dict[tuple[int, bool], dict[SetScore, float]] = {}
    while states:
        following: dict[tuple[int, int, int, int, int], float] = defaultdict(float)
        for (sa, sb, server, ga, gb), mass in states.items():
            deciding = sa == need - 1 and sb == need - 1
            key = (server, deciding)
            if key not in cache:
                cache[key] = set_distribution(pa, pb, first=server, fmt=fmt, deciding=deciding)
            for score, weight in cache[key].items():
                na, nb = sa + (score[0] > score[1]), sb + (score[1] > score[0])
                state = (na, nb, ga + score[0], gb + score[1])
                if na == need or nb == need:
                    finished[state] += mass * weight
                else:
                    following[(na, nb, next_first(server, score), state[2], state[3])] += (
                        mass * weight
                    )
        states = following
    correct: dict[tuple[int, int], float] = defaultdict(float)
    totals: dict[int, float] = defaultdict(float)
    margins: dict[int, float] = defaultdict(float)
    for (sa, sb, ga, gb), mass in finished.items():
        correct[(sa, sb)] += mass
        totals[ga + gb] += mass
        margins[ga - gb] += mass
    win = sum(mass for (sa, _), mass in correct.items() if sa == need)
    result = MatchDistribution(fmt.version, win, dict(correct), dict(totals), dict(margins))
    if abs(result.mass - 1.0) > TOLERANCE:
        raise NonConvergence(f"Distribution mass {result.mass} differs from 1")
    return result


@lru_cache(maxsize=4096)
def _cached(pa: float, pb: float, first: int, fmt: MatchFormat) -> MatchDistribution:
    return _match(pa, pb, first, fmt)


def _average[K](left: dict[K, float], right: dict[K, float]) -> dict[K, float]:
    return {key: (left.get(key, 0.0) + right.get(key, 0.0)) / 2 for key in set(left) | set(right)}


def match_distribution(
    pa: float, pb: float, fmt: MatchFormat, *, first: int | None = None
) -> MatchDistribution:
    """Exact distribution. ``first=None`` averages the two possible first servers."""
    _check(pa), _check(pb)
    if first is not None:
        return _cached(pa, pb, first, fmt)
    one, two = _cached(pa, pb, 0, fmt), _cached(pa, pb, 1, fmt)

    return MatchDistribution(
        fmt.version,
        (one.win_a + two.win_a) / 2,
        _average(one.correct_score, two.correct_score),
        _average(one.total_games, two.total_games),
        _average(one.game_margin, two.game_margin),
    )
