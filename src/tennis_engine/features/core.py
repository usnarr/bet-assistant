"""F08 core feature set ``core-v1``: context, Elo, form and serve/return.

Orientation follows the canonical match order: ``p1`` is ``match.player_ids[0]``.
``diff.*`` is ``p1 - p2``, so a player swap negates it and leaves ``match.*`` unchanged.
"""

from decimal import Decimal
from typing import cast

from tennis_engine.contracts.domain import FeatureValue
from tennis_engine.normalization.contracts import Surface

from .context import CONTEXT_DEFINITIONS, context_group
from .contracts import FeatureDefinition
from .ratings import EloConfig, FormConfig, RatingState, form, replay
from .serve_return import ServeReturnConfig, rates
from .snapshots import FeatureContext, FeatureSet, fixed

ELO = EloConfig()
FORM = FormConfig()
SERVE_RETURN = ServeReturnConfig()
SIDES = ("p1", "p2")


def _side_definitions(
    stem: str, unit: str, description: str, missing: str
) -> tuple[FeatureDefinition, ...]:
    return tuple(
        FeatureDefinition(
            name=f"{side}.{stem}",
            unit=unit,
            direction="PLAYER_ONE" if side == "p1" else "PLAYER_TWO",
            description=description,
            missing_behavior=missing,
        )
        for side in SIDES
    )


def _diff(stem: str, unit: str, missing: str) -> FeatureDefinition:
    return FeatureDefinition(
        name=f"diff.{stem}",
        unit=unit,
        direction="PLAYER_ONE_MINUS_TWO",
        description=f"p1.{stem} - p2.{stem}",
        missing_behavior=missing,
    )


PRIOR = "Initial rating with a 0 support count"
RATING_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    *_side_definitions("elo", "Elo points", "Global Elo after inactivity shrinkage", PRIOR),
    _diff("elo", "Elo points", PRIOR),
    *_side_definitions("elo_matches", "matches", "Matches used by global Elo", "0 = none"),
    *_side_definitions(
        "surface_elo", "Elo points", "Elo on this edition's surface", "None if surface UNKNOWN"
    ),
    _diff("surface_elo", "Elo points", "None if surface UNKNOWN"),
    *_side_definitions(
        "surface_elo_matches", "matches", "Matches used by surface Elo", "None if UNKNOWN"
    ),
)
FORM_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    *_side_definitions(
        "form", "score residual", "Shrunk half-life mean of won - expected", "None if no matches"
    ),
    _diff("form", "score residual", "None if either form is missing"),
    *_side_definitions("form_ess", "matches", "Effective sample size of form weights", "0"),
    *_side_definitions("form_matches", "matches", "Matches in the form window", "0"),
)
SERVE_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    *_side_definitions(
        "serve_rate", "probability", "Beta-Binomial shrunk serve point-win rate", "Prior mean"
    ),
    *_side_definitions(
        "serve_rate_raw", "probability", "Raw serve point-win rate", "None if 0 attempts"
    ),
    *_side_definitions("serve_points", "points", "Serve points observed in the window", "0"),
    *_side_definitions(
        "return_rate", "probability", "Beta-Binomial shrunk return point-win rate", "Prior mean"
    ),
    *_side_definitions(
        "return_rate_raw", "probability", "Raw return point-win rate", "None if 0 attempts"
    ),
    *_side_definitions("return_points", "points", "Return points observed", "0"),
    *_side_definitions(
        "stats_missing", "flag", "True if no match in the window has serve stats", "never None"
    ),
    *_side_definitions(
        "stats_coverage",
        "fraction",
        "Matches with stats / matches in the window",
        "None if no matches in the window",
    ),
)


def _state(context: FeatureContext) -> RatingState:
    if "ratings" not in context.memo:
        state = replay(context.view, ELO, exclude=context.match.match_id)
        for item in state.rated:
            context.use(item.input)
        context.memo["ratings"] = state
    return cast(RatingState, context.memo["ratings"])


def rating_group(context: FeatureContext) -> dict[str, FeatureValue]:
    state = _state(context)
    surface = context.edition.surface
    values: dict[str, FeatureValue] = {}
    elo: list[Decimal] = []
    surface_elo: list[Decimal | None] = []
    for side, player in zip(SIDES, context.players, strict=True):
        entry = state.global_table.current(player.player_id, context.as_of)
        elo.append(entry.rating)
        values[f"{side}.elo"] = fixed(entry.rating)
        values[f"{side}.elo_matches"] = entry.matches
        if surface == Surface.UNKNOWN:
            surface_elo.append(None)
            values[f"{side}.surface_elo"] = None
            values[f"{side}.surface_elo_matches"] = None
            continue
        table = state.surface_tables.get(surface)
        rated = table.current(player.player_id, context.as_of) if table else None
        rating = rated.rating if rated else ELO.initial
        surface_elo.append(rating)
        values[f"{side}.surface_elo"] = fixed(rating)
        values[f"{side}.surface_elo_matches"] = rated.matches if rated else 0
    values["diff.elo"] = fixed(elo[0] - elo[1])
    first, second = surface_elo
    values["diff.surface_elo"] = (
        fixed(first - second) if first is not None and second is not None else None
    )
    return values


def form_group(context: FeatureContext) -> dict[str, FeatureValue]:
    state = _state(context)
    values: dict[str, FeatureValue] = {}
    forms = []
    for side, player in zip(SIDES, context.players, strict=True):
        result = form(state.played.get(player.player_id, []), context.as_of, FORM)
        forms.append(result.value)
        values[f"{side}.form"] = fixed(result.value) if result.value is not None else None
        values[f"{side}.form_ess"] = fixed(result.effective_sample_size)
        values[f"{side}.form_matches"] = result.matches
    first, second = forms
    values["diff.form"] = (
        fixed(first - second) if first is not None and second is not None else None
    )
    return values


def serve_return_group(context: FeatureContext) -> dict[str, FeatureValue]:
    values: dict[str, FeatureValue] = {}
    for side, player in zip(SIDES, context.players, strict=True):
        history = context.view.completed_matches(player.player_id, exclude=context.match.match_id)
        serve, ret, refs = rates(context.view, history, player.player_id, player.tour, SERVE_RETURN)
        for ref in refs:
            context.use(ref)
        values[f"{side}.serve_rate"] = fixed(serve.shrunk)
        values[f"{side}.serve_rate_raw"] = fixed(serve.raw) if serve.raw is not None else None
        values[f"{side}.serve_points"] = serve.attempts
        values[f"{side}.return_rate"] = fixed(ret.shrunk)
        values[f"{side}.return_rate_raw"] = fixed(ret.raw) if ret.raw is not None else None
        values[f"{side}.return_points"] = ret.attempts
        values[f"{side}.stats_missing"] = serve.missing
        window = serve.matches_with_stats + serve.matches_without_stats
        values[f"{side}.stats_coverage"] = (
            fixed(Decimal(serve.matches_with_stats) / Decimal(window)) if window else None
        )
    return values


CORE_SET = FeatureSet(
    version="core-v1",
    definitions=CONTEXT_DEFINITIONS + RATING_DEFINITIONS + FORM_DEFINITIONS + SERVE_DEFINITIONS,
    groups=(
        ("context", context_group),
        ("ratings", rating_group),
        ("form", form_group),
        ("serve_return", serve_return_group),
    ),
    config={
        "elo": ELO.model_dump_json(),
        "form": FORM.model_dump_json(),
        "serve_return": SERVE_RETURN.model_dump_json(),
    },
)


def swap_values(values: dict[str, FeatureValue]) -> dict[str, FeatureValue]:
    """Values for the reversed orientation: exchange p1/p2 and negate ``diff.*``."""
    swapped: dict[str, FeatureValue] = {}
    for name, value in values.items():
        if name.startswith("p1."):
            swapped["p2." + name[3:]] = value
        elif name.startswith("p2."):
            swapped["p1." + name[3:]] = value
        elif name.startswith("diff.") and isinstance(value, Decimal):
            swapped[name] = -value if value else value
        else:
            swapped[name] = value
    return swapped
