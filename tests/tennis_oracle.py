"""Independent MOD-02 oracle: plain recursion over points with a deep truncation.

It shares no code with ``models.point.exact``. Deuce and tiebreak ties are followed
point by point up to ``LIMIT`` points; the truncated mass is below 1e-15 for the
probabilities used in the fixtures.
"""

from functools import cache

LIMIT = 120
TIEBREAK_PATTERN = (0, 1, 1, 0)  # Relative server for points 1, 2, 3, 4, then repeat.


def game(p: float, advantage: bool = True) -> float:
    @cache
    def win(i: int, j: int) -> float:
        if i + j > LIMIT:
            return 0.5
        if not advantage and i == 3 and j == 3:
            return p
        if i >= 4 and i - j >= 2:
            return 1.0
        if j >= 4 and j - i >= 2:
            return 0.0
        return p * win(i + 1, j) + (1 - p) * win(i, j + 1)

    return win(0, 0)


def tiebreak_server(index: int, first: int) -> int:
    relative = TIEBREAK_PATTERN[index % 4]
    return first if relative == 0 else 1 - first


def tiebreak(pa: float, pb: float, target: int, first: int) -> float:
    @cache
    def win(i: int, j: int) -> float:
        if i + j > LIMIT:
            return 0.5
        if i >= target and i - j >= 2:
            return 1.0
        if j >= target and j - i >= 2:
            return 0.0
        a_point = pa if tiebreak_server(i + j, first) == 0 else 1 - pb
        return a_point * win(i + 1, j) + (1 - a_point) * win(i, j + 1)

    return win(0, 0)


def set_scores(
    pa: float, pb: float, first: int, target: int = 7, match_tiebreak: bool = False
) -> dict[tuple[int, int], float]:
    if match_tiebreak:
        a = tiebreak(pa, pb, 10, first)
        return {(1, 0): a, (0, 1): 1 - a}
    ha, hb = game(pa), game(pb)
    out: dict[tuple[int, int], float] = {}

    def walk(a: int, b: int, mass: float) -> None:
        if (a >= 6 and a - b >= 2) or (b >= 6 and b - a >= 2):
            out[(a, b)] = out.get((a, b), 0.0) + mass
            return
        if a == 6 and b == 6:
            won = tiebreak(pa, pb, target, first)
            out[(7, 6)] = out.get((7, 6), 0.0) + mass * won
            out[(6, 7)] = out.get((6, 7), 0.0) + mass * (1 - won)
            return
        server = first if (a + b) % 2 == 0 else 1 - first
        a_game = ha if server == 0 else 1 - hb
        walk(a + 1, b, mass * a_game)
        walk(a, b + 1, mass * (1 - a_game))

    walk(0, 0, 1.0)
    return out


def match(
    pa: float, pb: float, first: int, final_target: int = 7, match_tiebreak: bool = False
) -> dict[tuple[int, int], float]:
    """Correct-score distribution of a best-of-three match."""
    out: dict[tuple[int, int], float] = {}

    def walk(sa: int, sb: int, server: int, mass: float) -> None:
        if sa == 2 or sb == 2:
            out[(sa, sb)] = out.get((sa, sb), 0.0) + mass
            return
        deciding = sa == 1 and sb == 1
        scores = set_scores(
            pa,
            pb,
            server,
            final_target if deciding else 7,
            match_tiebreak and deciding,
        )
        for (a, b), weight in scores.items():
            following = server if (a + b) % 2 == 0 else 1 - server
            walk(sa + (a > b), sb + (b > a), following, mass * weight)

    walk(0, 0, first, 1.0)
    return out
