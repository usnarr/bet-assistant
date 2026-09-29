"""Structural and semantic validators for source sports records (F04.1).

Errors reject a record to review/dead letter. Flags keep a valid record but mark it as
outside the supported scope; they never become a guessed default.
"""

from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
    BestOf,
    DrawType,
    MatchStatus,
    ServeReturnCounts,
    SetScore,
    SourceMatchRecord,
    SourceTournamentRecord,
)


class Severity(StrEnum):
    ERROR = "ERROR"
    FLAG = "FLAG"


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str


def _error(code: str, message: str) -> Issue:
    return Issue(Severity.ERROR, code, message)


def _flag(code: str, message: str) -> Issue:
    return Issue(Severity.FLAG, code, message)


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def validate_tournament(record: SourceTournamentRecord) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    if record.tour is None:
        issues.append(_error("UNKNOWN_TOUR", "Tournament tour is not ATP or WTA"))
    if record.timezone is not None and not valid_timezone(record.timezone):
        issues.append(_error("INVALID_TIMEZONE", f"Unknown IANA timezone {record.timezone!r}"))
    if record.timezone is None:
        issues.append(_flag("MISSING_TIMEZONE", "Local tournament time cannot be derived"))
    if record.start_date and record.end_date and record.end_date < record.start_date:
        issues.append(_error("INVALID_DATES", "Tournament ends before it starts"))
    return tuple(issues)


def _tiebreak_complete(points: tuple[int, int]) -> bool:
    high, low = max(points), min(points)
    # Target is 7 (set tiebreak) or 10 (match tiebreak); the rule is not inferred here.
    return high >= 7 and high - low >= 2 and (high in {7, 10} or high - low == 2)


def set_winner(score: SetScore) -> int | None:
    """Return the winning index of a completed set, or ``None`` if the set is unfinished."""
    a, b = score.games
    high, low = max(a, b), min(a, b)
    winner = 0 if a > b else 1
    if high == 7 and low == 6:
        if score.tiebreak_points is None or not _tiebreak_complete(score.tiebreak_points):
            return None
        tb_winner = 0 if score.tiebreak_points[0] > score.tiebreak_points[1] else 1
        return winner if tb_winner == winner else None
    if score.tiebreak_points is not None:
        # A match tiebreak played in lieu of a final set is recorded as 1-0 with points.
        points = score.tiebreak_points
        if high == 1 and low == 0 and max(points) >= 10 and _tiebreak_complete(points):
            return winner if (points[0] > points[1]) == (a > b) else None
        return None
    if high == 6 and low <= 4:
        return winner
    if high >= 7 and high - low == 2:
        return winner
    return None


def _set_is_legal_partial(score: SetScore) -> bool:
    a, b = score.games
    high, low = max(a, b), min(a, b)
    if score.tiebreak_points is not None:
        return high == 6 and low == 6
    return high <= 6 or high - low <= 1


def validate_match(record: SourceMatchRecord) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    first, second = record.participant_ids
    if first == second:
        issues.append(_error("SAME_PARTICIPANT", "A match requires two distinct participants"))
    if record.tour is None:
        issues.append(_error("UNKNOWN_TOUR", "Match tour is not ATP or WTA"))
    if record.draw_type != DrawType.SINGLES:
        issues.append(_flag("UNSUPPORTED_DRAW", "Only singles are supported"))
    if record.best_of == BestOf.UNKNOWN:
        issues.append(_flag("UNKNOWN_FORMAT", "Best-of format is unknown; do not infer it"))
    if record.actual_start and record.actual_end and record.actual_end < record.actual_start:
        issues.append(_error("INVALID_TIMES", "Match ends before it starts"))

    status = record.status
    if status.has_winner:
        if record.winner_id is None:
            issues.append(_error("MISSING_WINNER", f"{status} requires a winner"))
        elif record.winner_id not in record.participant_ids:
            issues.append(_error("INVALID_WINNER", "Winner is not a participant"))
    elif record.winner_id is not None:
        issues.append(_error("UNEXPECTED_WINNER", f"{status} cannot have a winner"))
    if status in {MatchStatus.SCHEDULED, MatchStatus.CANCELLED, MatchStatus.POSTPONED}:
        if record.sets:
            issues.append(_error("UNEXPECTED_SCORE", f"{status} cannot have set scores"))
    if status == MatchStatus.WALKOVER and record.sets:
        issues.append(_error("UNEXPECTED_SCORE", "A walkover has no played sets"))
    if status in {MatchStatus.COMPLETED, MatchStatus.RETIRED, MatchStatus.DEFAULTED}:
        issues.extend(_validate_score(record))
    for index, counts in enumerate(record.stats):
        opponent = record.stats[1 - index]
        if counts is None or opponent is None:
            continue
        if _serve_return_conflict(counts, opponent):
            issues.append(
                _error("INCONSISTENT_STATS", "Serve counts disagree with opponent returns")
            )
    return tuple(issues)


def _serve_return_conflict(server: ServeReturnCounts, returner: ServeReturnCounts) -> bool:
    played, faced = server.serve_points, returner.return_points
    if played is not None and faced is not None and played != faced:
        return True
    won, return_won = server.serve_points_won, returner.return_points_won
    if won is not None and faced is not None and return_won is not None:
        return won != faced - return_won
    return False


def _validate_score(record: SourceMatchRecord) -> list[Issue]:
    issues: list[Issue] = []
    if record.winner_id is None or record.winner_id not in record.participant_ids:
        return issues
    winner_index = record.participant_ids.index(record.winner_id)
    needed = record.best_of.sets_to_win
    won = [0, 0]
    for position, score in enumerate(record.sets):
        result = set_winner(score)
        last = position == len(record.sets) - 1
        if result is None:
            if record.status == MatchStatus.COMPLETED or not last:
                issues.append(_error("ILLEGAL_SET", f"Set {position + 1} score is not legal"))
                return issues
            if not _set_is_legal_partial(score):
                issues.append(_error("ILLEGAL_SET", f"Set {position + 1} partial score"))
                return issues
            continue
        won[result] += 1
        if needed is not None and max(won) == needed and not last:
            issues.append(_error("SETS_AFTER_END", "Sets continue after the match was decided"))
            return issues
    if record.status == MatchStatus.COMPLETED:
        if not record.sets:
            issues.append(_error("MISSING_SCORE", "A completed match requires set scores"))
        elif needed is not None and won[winner_index] != needed:
            issues.append(_error("SCORE_WINNER_MISMATCH", "Score does not decide the winner"))
        elif needed is None and won[winner_index] <= won[1 - winner_index]:
            issues.append(_error("SCORE_WINNER_MISMATCH", "Winner did not win more sets"))
    elif needed is not None and max(won) >= needed:
        issues.append(_error("RETIREMENT_AFTER_END", "A decided match cannot end by retirement"))
    return issues


def errors(issues: tuple[Issue, ...]) -> tuple[Issue, ...]:
    return tuple(issue for issue in issues if issue.severity == Severity.ERROR)
