"""Core context and workload features (F08 first implementation rows).

Direction: ``p1``/``p2`` follow the canonical match orientation. ``diff`` is ``p1 - p2``.
Missing inputs produce ``None`` and an explicit missing flag, never a filled default.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from tennis_engine.contracts.domain import FeatureValue

from .contracts import FeatureDefinition
from .snapshots import FeatureContext, FeatureSet, fixed

WORKLOAD_WINDOWS = (7, 14, 28)
DAYS_PER_YEAR = Decimal("365.2425")
SECONDS_PER_DAY = Decimal(86400)


def _definition(
    name: str, unit: str, direction: str, description: str, **extra: str
) -> FeatureDefinition:
    return FeatureDefinition.model_validate(
        {
            "name": name,
            "unit": unit,
            "direction": direction,
            "description": description,
            "missing_behavior": extra.pop("missing_behavior", "None plus missing flag"),
            **extra,
        }
    )


CONTEXT_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    _definition("match.tour", "code", "MATCH", "Tour of the match"),
    _definition("match.surface", "code", "MATCH", "Edition surface; UNKNOWN stays UNKNOWN"),
    _definition("match.environment", "code", "MATCH", "Indoor/outdoor; UNKNOWN stays UNKNOWN"),
    _definition("match.best_of", "code", "MATCH", "Best-of format; never inferred"),
    _definition("match.round", "code", "MATCH", "Draw round"),
    _definition("match.draw_stage", "code", "MATCH", "Qualifying or main draw"),
    *(
        _definition(
            f"{side}.rank",
            "rank",
            "PLAYER_ONE" if side == "p1" else "PLAYER_TWO",
            "Latest ranking dated and observed by the cutoff",
            source_requirement="ranking snapshot",
        )
        for side in ("p1", "p2")
    ),
    _definition(
        "diff.log_rank",
        "log(rank)",
        "PLAYER_ONE_MINUS_TWO",
        "log(rank p1) - log(rank p2)",
        missing_behavior="None if either rank is missing",
    ),
    *(
        _definition(
            f"{side}.age_years",
            "years",
            "PLAYER_ONE" if side == "p1" else "PLAYER_TWO",
            "Age at the scheduled start known at the cutoff",
        )
        for side in ("p1", "p2")
    ),
    *(
        _definition(
            f"{side}.days_since_last_match",
            "days",
            "PLAYER_ONE" if side == "p1" else "PLAYER_TWO",
            "Days between the last completed match end and the cutoff",
            missing_behavior="None when no completed match is known",
        )
        for side in ("p1", "p2")
    ),
    *(
        _definition(
            f"{side}.matches_{days}d",
            "matches",
            "PLAYER_ONE" if side == "p1" else "PLAYER_TWO",
            f"Completed matches in the {days} days before the cutoff",
            window=f"{days}d",
            missing_behavior="0 is a real count of known matches",
        )
        for side in ("p1", "p2")
        for days in WORKLOAD_WINDOWS
    ),
    *(
        _definition(
            f"{side}.history_matches",
            "matches",
            "PLAYER_ONE" if side == "p1" else "PLAYER_TWO",
            "Completed matches known at the cutoff",
            missing_behavior="0 means none",
        )
        for side in ("p1", "p2")
    ),
)


def days_between(earlier: datetime, later: datetime) -> Decimal:
    delta = later - earlier
    return fixed(Decimal(delta.days) + Decimal(delta.seconds) / SECONDS_PER_DAY)


def context_group(context: FeatureContext) -> dict[str, FeatureValue]:
    match, edition = context.match, context.edition
    view = context.view
    values: dict[str, FeatureValue] = {}
    known = context.static_ref("match", str(match.match_id), match.created_at, "canonical")
    values["match.tour"] = match.tour.value if known else None
    values["match.surface"] = edition.surface.value if known else None
    values["match.environment"] = edition.environment.value if known else None
    values["match.best_of"] = match.best_of.value if known else None
    values["match.round"] = match.round.value if known else None
    values["match.draw_stage"] = match.draw_stage.value if known else None

    schedule = view.schedule(match.match_id)
    start = None
    if schedule is not None:
        context.use(schedule[1])
        start = schedule[0].scheduled_start

    ranks: list[int | None] = []
    for side, player in zip(("p1", "p2"), context.players, strict=True):
        ranking = view.ranking(player.player_id)
        rank = None
        if ranking is not None:
            context.use(ranking[1])
            rank = ranking[0].rank
        ranks.append(rank)
        values[f"{side}.rank"] = rank

        age = None
        if (
            player.birth_date is not None
            and start is not None
            and context.static_ref("player", str(player.player_id), player.created_at, "canonical")
        ):
            age = fixed(Decimal((start.date() - player.birth_date).days) / DAYS_PER_YEAR)
        values[f"{side}.age_years"] = age

        history = view.completed_matches(player.player_id, exclude=match.match_id)
        for item in history:
            context.use(item.input)
        values[f"{side}.history_matches"] = len(history)
        values[f"{side}.days_since_last_match"] = (
            days_between(history[-1].ended_at, context.as_of) if history else None
        )
        for days in WORKLOAD_WINDOWS:
            since = context.as_of - timedelta(days=days)
            values[f"{side}.matches_{days}d"] = sum(1 for item in history if item.ended_at >= since)

    if ranks[0] is not None and ranks[1] is not None:
        values["diff.log_rank"] = fixed(Decimal(ranks[0]).ln() - Decimal(ranks[1]).ln())
    else:
        values["diff.log_rank"] = None
    return values


CORE_CONTEXT_SET = FeatureSet(
    version="core-context-v1",
    definitions=CONTEXT_DEFINITIONS,
    groups=(("context", context_group),),
)
