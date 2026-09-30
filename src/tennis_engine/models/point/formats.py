"""Versioned tennis match formats (F10 contracts).

Only formats listed in ``VERIFIED_FORMATS`` are enabled. A format that is not verified for
a tournament must not be approximated by a similar one; the caller abstains instead.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from tennis_engine.common.contracts import Contract, Identifier


class DecidingSet(StrEnum):
    """How the final set is decided."""

    TIEBREAK_7 = "TIEBREAK_7"  # Normal set with a 7-point tiebreak at 6-6.
    TIEBREAK_10 = "TIEBREAK_10"  # Normal set with a 10-point tiebreak at 6-6.
    MATCH_TIEBREAK_10 = "MATCH_TIEBREAK_10"  # A 10-point tiebreak replaces the final set.
    ADVANTAGE = "ADVANTAGE"  # No tiebreak; win by two games.


class MatchFormat(Contract):
    version: Identifier
    best_of: Literal[3, 5]
    advantage_games: bool = True
    set_games: Annotated[int, Field(ge=1, le=10, strict=True)] = 6
    tiebreak_target: Annotated[int, Field(ge=5, le=12, strict=True)] = 7
    deciding_set: DecidingSet = DecidingSet.TIEBREAK_7
    verified: bool = False

    @property
    def sets_to_win(self) -> int:
        return self.best_of // 2 + 1


BEST_OF_3_STANDARD = MatchFormat(
    version="bo3-tb7-v1",
    best_of=3,
    deciding_set=DecidingSet.TIEBREAK_7,
    verified=True,
)
BEST_OF_3_FINAL_TB10 = MatchFormat(
    version="bo3-final-tb10-v1",
    best_of=3,
    deciding_set=DecidingSet.TIEBREAK_10,
    verified=True,
)
BEST_OF_3_MATCH_TIEBREAK = MatchFormat(
    version="bo3-match-tb10-v1",
    best_of=3,
    deciding_set=DecidingSet.MATCH_TIEBREAK_10,
    verified=False,
)
BEST_OF_5_FINAL_TB10 = MatchFormat(
    version="bo5-final-tb10-v1",
    best_of=5,
    deciding_set=DecidingSet.TIEBREAK_10,
    verified=False,
)

VERIFIED_FORMATS = {
    item.version: item
    for item in (BEST_OF_3_STANDARD, BEST_OF_3_FINAL_TB10, BEST_OF_3_MATCH_TIEBREAK)
    if item.verified
}
ALL_FORMATS = {
    item.version: item
    for item in (
        BEST_OF_3_STANDARD,
        BEST_OF_3_FINAL_TB10,
        BEST_OF_3_MATCH_TIEBREAK,
        BEST_OF_5_FINAL_TB10,
    )
}


class UnsupportedFormat(ValueError):
    pass


def enabled(version: str) -> MatchFormat:
    """Return a verified format or raise; best-of-five stays gated by F17."""
    try:
        return VERIFIED_FORMATS[version]
    except KeyError as error:
        raise UnsupportedFormat(f"Format {version!r} is not verified") from error
