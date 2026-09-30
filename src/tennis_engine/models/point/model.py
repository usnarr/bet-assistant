"""Regularized serve/return point model and match predictions (F10.1, F10.5).

``logit P(server i wins a point against returner j) = mu[tour, surface] + s_i - r_j``.

Server and returner effects have a Gaussian prior (L2 penalty) centred on zero, so a
player with little data stays near the tour/surface mean. Counts are recency weighted.
The fit uses only stats that :class:`AsOfView` proves available at the training cutoff.
Parameter uncertainty uses independent normal draws from a diagonal Laplace
approximation; this ignores parameter correlation and is labelled as such.
"""

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, Identifier, Probability, Timestamp
from tennis_engine.features.asof import AsOfView
from tennis_engine.features.contracts import AvailabilityMode, digest
from tennis_engine.normalization.contracts import MatchStatus
from tennis_engine.normalization.store import IdentityStore

from ..baselines.contracts import SupportStatus
from .exact import match_distribution
from .formats import MatchFormat

QUANTUM = Decimal("1e-9")


class PointModelConfig(Contract):
    version: Identifier = "point-v1-candidate"
    # Prior precision on logit effects; 10 means a prior sd of about 0.32.
    effect_penalty: float = Field(default=10.0, gt=0)
    mean_penalty: float = Field(default=1e-6, gt=0)
    half_life_days: float = Field(default=180.0, gt=0)
    min_weighted_points: float = Field(default=150.0, ge=0)
    max_sweeps: int = Field(default=500, ge=1)
    tolerance: float = Field(default=1e-10, gt=0)
    draws: int = Field(default=200, ge=0)
    seed: int = 20260930


@dataclass(frozen=True)
class ServeObservation:
    server: UUID
    returner: UUID
    group: str
    won: int
    played: int
    weight: float


def observations(
    store: IdentityStore, as_of: datetime, config: PointModelConfig
) -> list[ServeObservation]:
    """Serve counts from completed matches known at ``as_of`` (prospective mode)."""
    view = AsOfView(store, as_of, AvailabilityMode.PROSPECTIVE)
    rows = []
    for item in view.all_completed():
        if item.result.status == MatchStatus.WALKOVER:
            continue
        surface = store.edition(item.match.edition_id).surface.value
        group = f"{item.match.tour.value}:{surface}"
        age = (as_of - item.ended_at).total_seconds() / 86400
        weight = 0.5 ** (age / config.half_life_days)
        first, second = item.match.player_ids
        for server, returner in ((first, second), (second, first)):
            known = view.stats(item.match.match_id, server)
            if known is None:
                continue
            counts = known[0].counts
            if counts.serve_points is None or counts.serve_points_won is None:
                continue
            if counts.serve_points == 0:
                continue
            rows.append(
                ServeObservation(
                    server, returner, group, counts.serve_points_won, counts.serve_points, weight
                )
            )
    return rows


class PointModelArtifact(Contract):
    schema_version: Literal["1.0"] = "1.0"
    version: Identifier
    training_cutoff: Timestamp
    means: dict[str, float]
    serve: dict[UUID, float]
    returns: dict[UUID, float]
    mean_variance: dict[str, float]
    serve_variance: dict[UUID, float]
    return_variance: dict[UUID, float]
    weighted_points: dict[UUID, float]
    observations: int
    sweeps: int
    converged: bool
    config: PointModelConfig
    artifact_sha256: str

    @model_validator(mode="after")
    def converged_fit(self) -> Self:
        if not self.converged:
            raise ValueError("A non-converged point model cannot become an artifact")
        return self


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


def fit(
    store: IdentityStore, *, training_cutoff: datetime, config: PointModelConfig | None = None
) -> PointModelArtifact:
    config = config or PointModelConfig()
    rows = observations(store, training_cutoff, config)
    if not rows:
        raise ValueError("No serve counts are known at the training cutoff; BLOCKED")
    means: dict[str, float] = defaultdict(float)
    serve: dict[UUID, float] = defaultdict(float)
    returns: dict[UUID, float] = defaultdict(float)
    for row in rows:
        means[row.group] += 0.0
        serve[row.server] += 0.0
        returns[row.returner] += 0.0
    # Start each mean at its pooled logit rate.
    for group in means:
        won = sum(row.won * row.weight for row in rows if row.group == group)
        played = sum(row.played * row.weight for row in rows if row.group == group)
        rate = min(max(won / played, 1e-6), 1 - 1e-6)
        means[group] = math.log(rate / (1 - rate))

    def sweep_parameter[K](
        table: dict[K, float], key: K, sign: float, selector: str, penalty: float
    ) -> tuple[float, float]:
        gradient = penalty * table[key]
        hessian = penalty
        for row in rows:
            if getattr(row, selector) != key:
                continue
            p = _sigmoid(means[row.group] + serve[row.server] - returns[row.returner])
            gradient += row.weight * (row.played * p - row.won) * sign
            hessian += row.weight * row.played * p * (1 - p)
        step = gradient / hessian
        table[key] -= step
        return abs(step), hessian

    hessians: dict[tuple[str, object], float] = {}
    converged = False
    sweeps = 0
    while sweeps < config.max_sweeps:
        sweeps += 1
        largest = 0.0
        for group in sorted(means):
            step, h = sweep_parameter(means, group, 1.0, "group", config.mean_penalty)
            largest, hessians[("mean", group)] = max(largest, step), h
        for player in sorted(serve, key=str):
            step, h = sweep_parameter(serve, player, 1.0, "server", config.effect_penalty)
            largest, hessians[("serve", player)] = max(largest, step), h
        for player in sorted(returns, key=str):
            step, h = sweep_parameter(returns, player, -1.0, "returner", config.effect_penalty)
            largest, hessians[("return", player)] = max(largest, step), h
        if largest < config.tolerance:
            converged = True
            break
    points: dict[UUID, float] = defaultdict(float)
    for row in rows:
        points[row.server] += row.played * row.weight
    body = {
        "version": config.version,
        "cutoff": training_cutoff.isoformat(),
        "means": {key: round(value, 12) for key, value in sorted(means.items())},
        "serve": {str(key): round(value, 12) for key, value in sorted(serve.items(), key=str)},
        "returns": {str(key): round(value, 12) for key, value in sorted(returns.items(), key=str)},
        "config": config.model_dump(mode="json"),
    }
    return PointModelArtifact(
        version=config.version,
        training_cutoff=training_cutoff,
        means=dict(means),
        serve=dict(serve),
        returns=dict(returns),
        mean_variance={key: 1 / hessians[("mean", key)] for key in means},
        serve_variance={key: 1 / hessians[("serve", key)] for key in serve},
        return_variance={key: 1 / hessians[("return", key)] for key in returns},
        weighted_points=dict(points),
        observations=len(rows),
        sweeps=sweeps,
        converged=converged,
        config=config,
        artifact_sha256=digest(body),
    )


class PointPrediction(Contract):
    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    player_ids: tuple[UUID, UUID]
    as_of: Timestamp
    model_version: Identifier
    artifact_sha256: str
    training_cutoff: Timestamp
    format_version: Identifier | None
    support: SupportStatus
    reasons: tuple[Identifier, ...] = ()
    serve_point_probabilities: tuple[Probability, Probability] | None
    probability_player_one: Probability | None
    draws: int
    seed: int
    draw_lower: Probability | None = None
    draw_upper: Probability | None = None
    draw_standard_error: Decimal | None = None
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def unsupported_has_no_probability(self) -> Self:
        if (self.support == SupportStatus.UNSUPPORTED) != (self.probability_player_one is None):
            raise ValueError("Only an unsupported prediction lacks a probability")
        if self.training_cutoff > self.as_of:
            raise ValueError("Training data must end at or before the prediction cutoff")
        return self


LIMITATIONS = (
    "Independent identically distributed points; no momentum or pressure effects",
    "Diagonal Laplace draws ignore parameter correlation",
    "Draw spread is not a confidence interval for the true win probability",
)


def _probabilities(
    artifact: PointModelArtifact, group: str, a: UUID, b: UUID
) -> tuple[float, float]:
    mean = artifact.means[group]
    pa = _sigmoid(mean + artifact.serve.get(a, 0.0) - artifact.returns.get(b, 0.0))
    pb = _sigmoid(mean + artifact.serve.get(b, 0.0) - artifact.returns.get(a, 0.0))
    return pa, pb


def predict(
    store: IdentityStore,
    match_id: UUID,
    artifact: PointModelArtifact,
    *,
    as_of: datetime,
    fmt: MatchFormat | None,
) -> PointPrediction:
    """Predict a canonical match. ``fmt=None`` means the format is not verified: abstain."""
    match = store.match(match_id)
    a, b = match.player_ids
    group = f"{match.tour.value}:{store.edition(match.edition_id).surface.value}"
    reasons: list[str] = []
    if fmt is None or not fmt.verified:
        reasons.append("format_unverified")
    if group not in artifact.means:
        reasons.append("no_tour_surface_prior")
    for player in (a, b):
        if artifact.weighted_points.get(player, 0.0) < artifact.config.min_weighted_points:
            reasons.append("sparse_serve_return")
            break
    common = {
        "match_id": match_id,
        "player_ids": match.player_ids,
        "as_of": as_of,
        "model_version": artifact.version,
        "artifact_sha256": artifact.artifact_sha256,
        "training_cutoff": artifact.training_cutoff,
        "format_version": fmt.version if fmt else None,
        "seed": artifact.config.seed,
        "limitations": LIMITATIONS,
    }
    if reasons:
        return PointPrediction(
            **common,
            support=SupportStatus.UNSUPPORTED,
            reasons=tuple(reasons),
            serve_point_probabilities=None,
            probability_player_one=None,
            draws=0,
        )
    assert fmt is not None
    pa, pb = _probabilities(artifact, group, a, b)
    central = match_distribution(pa, pb, fmt).win_a
    rng = random.Random(artifact.config.seed)
    values = []
    for _ in range(artifact.config.draws):
        mean = rng.gauss(artifact.means[group], math.sqrt(artifact.mean_variance[group]))

        def effect(table: dict[UUID, float], variances: dict[UUID, float], player: UUID) -> float:
            if player not in table:
                return rng.gauss(0.0, math.sqrt(1 / artifact.config.effect_penalty))
            return rng.gauss(table[player], math.sqrt(variances[player]))

        sa = effect(artifact.serve, artifact.serve_variance, a)
        ra = effect(artifact.returns, artifact.return_variance, a)
        sb = effect(artifact.serve, artifact.serve_variance, b)
        rb = effect(artifact.returns, artifact.return_variance, b)
        values.append(
            match_distribution(
                round(_sigmoid(mean + sa - rb), 6), round(_sigmoid(mean + sb - ra), 6), fmt
            ).win_a
        )
    extra: dict[str, Decimal] = {}
    if len(values) >= 2:
        ordered = sorted(values)
        average = sum(values) / len(values)
        spread = math.sqrt(sum((v - average) ** 2 for v in values) / (len(values) - 1))
        extra = {
            "draw_lower": Decimal(repr(ordered[int(0.05 * (len(ordered) - 1))])).quantize(QUANTUM),
            "draw_upper": Decimal(repr(ordered[math.ceil(0.95 * (len(ordered) - 1))])).quantize(
                QUANTUM
            ),
            "draw_standard_error": Decimal(repr(spread / math.sqrt(len(values)))).quantize(QUANTUM),
        }
    return PointPrediction(
        **common,
        support=SupportStatus.SUPPORTED,
        serve_point_probabilities=(
            Decimal(repr(pa)).quantize(QUANTUM),
            Decimal(repr(pb)).quantize(QUANTUM),
        ),
        probability_player_one=Decimal(repr(central)).quantize(QUANTUM),
        draws=len(values),
        **extra,
    )
