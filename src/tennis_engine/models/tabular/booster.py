"""F11.1 to F11.3 gradient-boosted tabular model (XGBoost, CPU build).

- Target: canonical player one wins. Training rows are completed, retired or defaulted
  matches whose label was known at the training cutoff (the harness selects them).
- Paired training: every row is also added in the reversed orientation with the reversed
  label. Prediction averages both orientations, ``(f(x) + 1 - f(swap(x))) / 2``, so a player
  swap gives exactly ``1 - p``.
- Nested chronological tuning (F11.2): every grid entry is scored on inner folds of the
  training period with log loss as the objective. Brier and a calibration slope are
  recorded as diagnostics. The lowest mean log loss wins; a tie keeps the earlier, simpler
  entry. The grid size may not exceed the declared search budget, and every trial is kept.
- Monotone constraints (F11.3) are a grid dimension, so constrained and unconstrained
  models are compared as experiments, not assumed.
- Training runs on one thread with a fixed seed, so a refit gives the same booster bytes.

The output has no spread. A calibrator bootstrap (F11.6) supplies the spread that F12
needs; without one, F12 gives no assessment.
"""

import hashlib
import math
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import numpy as np
import xgboost as xgb
from pydantic import Field

from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp
from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import FeatureValue
from tennis_engine.features.contracts import FeatureSnapshot, digest
from tennis_engine.features.core import swap_values
from tennis_engine.models.baselines.baseline import TrainingRow
from tennis_engine.models.baselines.logit import fit as fit_slope

from .folds import inner_folds
from .schema import MARKET_FEATURE, TabularSchema, build_schema, encode, market_logit

PROBABILITY_QUANTUM = Decimal("1e-9")
METRIC_QUANTUM = Decimal("1e-6")
EPSILON = 1e-12
# Features whose effect on player one's chance can only be non-decreasing.
MONOTONE_INCREASING = ("diff.elo", "diff.surface_elo", "diff.form", MARKET_FEATURE)
MarketSource = Callable[[UUID, datetime], Decimal | None]


class BoosterParams(Contract):
    max_depth: Annotated[int, Field(ge=1, le=8, strict=True)]
    learning_rate: Annotated[Decimal, Field(gt=0, le=1)]
    rounds: Annotated[int, Field(ge=1, le=2000, strict=True)]
    min_child_weight: Annotated[Decimal, Field(ge=0)]
    reg_lambda: Annotated[Decimal, Field(ge=0)]
    monotone: bool


def _grid() -> tuple[BoosterParams, ...]:
    """Candidate grid, simplest first. The values are candidates, not accepted policy."""
    shapes = (
        (2, "0.1", 100, "5", "1"),
        (3, "0.1", 100, "5", "1"),
        (2, "0.05", 250, "5", "1"),
        (4, "0.05", 200, "10", "5"),
    )
    return tuple(
        BoosterParams(
            max_depth=depth,
            learning_rate=Decimal(rate),
            rounds=rounds,
            min_child_weight=Decimal(weight),
            reg_lambda=Decimal(penalty),
            monotone=monotone,
        )
        for monotone in (False, True)
        for depth, rate, rounds, weight, penalty in shapes
    )


DEFAULT_GRID = _grid()


class TabularConfig(Contract):
    name: Identifier = "tabular-xgb"
    version: Identifier = "v1"
    grid: tuple[BoosterParams, ...] = DEFAULT_GRID
    search_budget: Annotated[int, Field(ge=1, strict=True)] = 8
    inner_folds: Annotated[int, Field(ge=1, le=10, strict=True)] = 3
    min_training_matches: Annotated[int, Field(ge=2, strict=True)] = 40
    seed: int = 20261002


class TuningTrial(Contract):
    params: BoosterParams
    fold_log_loss: tuple[Decimal, ...]
    log_loss: Decimal
    brier: Decimal
    calibration_slope: Decimal | None
    validation_rows: Annotated[int, Field(ge=0, strict=True)]


class TabularArtifact(Contract):
    schema_version: Literal["1.0"] = "1.0"
    model_id: UUID
    name: Identifier
    version: Identifier
    library: Literal["xgboost-cpu"] = "xgboost-cpu"
    library_version: str
    feature_schema: TabularSchema
    params: BoosterParams
    trials: tuple[TuningTrial, ...]
    selection_rule: Literal["lowest-inner-log-loss;tie:first"] = "lowest-inner-log-loss;tie:first"
    training_cutoff: Timestamp
    training_matches: Annotated[int, Field(ge=1, strict=True)]
    training_rows: Annotated[int, Field(ge=1, strict=True)]
    seed: int
    booster_json: str
    booster_sha256: Digest
    artifact_sha256: Digest


def _values(snapshot: FeatureSnapshot, market: MarketSource | None) -> dict[str, FeatureValue]:
    values = dict(snapshot.values)
    if market is not None:
        values[MARKET_FEATURE] = market_logit(market(snapshot.match_id, snapshot.as_of))
    return values


def _paired(
    rows: Sequence[TrainingRow], market: MarketSource | None
) -> list[tuple[dict[str, FeatureValue], int]]:
    paired = []
    for row in rows:
        values = _values(row.snapshot, market)
        outcome = int(row.label.player_one_won)
        paired.append((values, outcome))
        paired.append((swap_values(values), 1 - outcome))
    return paired


def _xgb_params(params: BoosterParams, schema: TabularSchema, seed: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "tree_method": "hist",
        "max_depth": params.max_depth,
        "eta": float(params.learning_rate),
        "min_child_weight": float(params.min_child_weight),
        "lambda": float(params.reg_lambda),
        "nthread": 1,
        "seed": seed,
        "verbosity": 0,
    }
    if params.monotone:
        signs = [1 if name in MONOTONE_INCREASING else 0 for name in schema.columns]
        result["monotone_constraints"] = "(" + ",".join(str(item) for item in signs) + ")"
    return result


def _train(
    samples: Sequence[tuple[dict[str, FeatureValue], int]],
    schema: TabularSchema,
    params: BoosterParams,
    seed: int,
) -> xgb.Booster:
    matrix = xgb.DMatrix(
        np.array([encode(values, schema) for values, _ in samples], dtype=np.float64),
        label=np.array([outcome for _, outcome in samples], dtype=np.float64),
        missing=math.nan,
        feature_names=list(schema.columns),
    )
    return xgb.train(_xgb_params(params, schema, seed), matrix, num_boost_round=params.rounds)


def _symmetric(
    booster: xgb.Booster, schema: TabularSchema, values: Sequence[dict[str, FeatureValue]]
) -> list[float]:
    """Player-swap-consistent probabilities for player one."""
    if not values:
        return []
    rows = [encode(item, schema) for item in values]
    rows += [encode(swap_values(item), schema) for item in values]
    matrix = xgb.DMatrix(
        np.array(rows, dtype=np.float64), missing=math.nan, feature_names=list(schema.columns)
    )
    raw = booster.predict(matrix)
    count = len(values)
    return [(float(raw[i]) + 1.0 - float(raw[count + i])) / 2.0 for i in range(count)]


def _log_loss(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    total = 0.0
    for p, y in zip(probabilities, outcomes, strict=True):
        p = min(max(p, EPSILON), 1 - EPSILON)
        total -= math.log(p) if y else math.log(1 - p)
    return total / len(outcomes)


def _decimal(value: float, quantum: Decimal = METRIC_QUANTUM) -> Decimal:
    return Decimal(repr(value)).quantize(quantum)


def _slope(probabilities: Sequence[float], outcomes: Sequence[int]) -> Decimal | None:
    xs = []
    for p in probabilities:
        p = min(max(p, 1e-6), 1 - 1e-6)
        xs.append(Decimal(repr(math.log(p / (1 - p)))))
    result = fit_slope(xs, list(outcomes), l2=Decimal("1e-6"), initial=Decimal(1))
    return result.coefficient.quantize(METRIC_QUANTUM) if result.converged else None


def _schema(
    rows: Sequence[TrainingRow], market: MarketSource | None
) -> tuple[TabularSchema, list[tuple[dict[str, FeatureValue], int]]]:
    samples = _paired(rows, market)
    reference = rows[0].snapshot
    schema = build_schema(
        [values for values, _ in samples],
        feature_set=reference.feature_set,
        feature_set_sha256=reference.feature_set_sha256,
        market_input=market is not None,
    )
    return schema, samples


def _tune(
    rows: Sequence[TrainingRow], config: TabularConfig, market: MarketSource | None
) -> tuple[TuningTrial, ...]:
    folds = inner_folds(rows, folds=config.inner_folds)
    prepared = []
    for fold in folds:
        schema, samples = _schema(fold.train, market)
        values = [_values(row.snapshot, market) for row in fold.validate]
        outcomes = [int(row.label.player_one_won) for row in fold.validate]
        prepared.append((schema, samples, values, outcomes))
    trials = []
    for params in config.grid:
        losses: list[float] = []
        predicted: list[float] = []
        observed: list[int] = []
        for schema, samples, values, outcomes in prepared:
            booster = _train(samples, schema, params, config.seed)
            probabilities = _symmetric(booster, schema, values)
            losses.append(_log_loss(probabilities, outcomes))
            predicted += probabilities
            observed += outcomes
        brier = sum((p - y) ** 2 for p, y in zip(predicted, observed, strict=True))
        trials.append(
            TuningTrial(
                params=params,
                fold_log_loss=tuple(_decimal(item) for item in losses),
                log_loss=_decimal(sum(losses) / len(losses)),
                brier=_decimal(brier / len(observed)),
                calibration_slope=_slope(predicted, observed),
                validation_rows=len(observed),
            )
        )
    return tuple(trials)


def train_tabular(
    rows: Sequence[TrainingRow],
    *,
    training_cutoff: datetime,
    config: TabularConfig | None = None,
    market: MarketSource | None = None,
) -> TabularArtifact:
    """Tune on inner folds, then refit the selected entry on every training row."""
    config = config or TabularConfig()
    if len(config.grid) > config.search_budget:
        raise ValueError("The grid is larger than the declared search budget")
    if not rows:
        raise ValueError("No training rows; the tabular model is BLOCKED")
    if len({(row.snapshot.feature_set, row.snapshot.feature_set_sha256) for row in rows}) != 1:
        raise ValueError("Training rows must share one feature set")
    for row in rows:
        if row.snapshot.as_of > training_cutoff or row.label.observed_at > training_cutoff:
            raise ValueError("A training row or label is later than the training cutoff")
    matches = {row.snapshot.match_id for row in rows}
    if len(matches) < config.min_training_matches:
        raise ValueError(
            f"{len(matches)} training matches, {config.min_training_matches} needed; BLOCKED"
        )
    trials = _tune(rows, config, market)
    best = min(range(len(trials)), key=lambda index: (trials[index].log_loss, index))
    params = trials[best].params
    schema, samples = _schema(rows, market)
    booster = _train(samples, schema, params, config.seed)
    raw = bytes(booster.save_raw("json"))
    booster_sha = hashlib.sha256(raw).hexdigest()
    body = {
        "name": config.name,
        "version": config.version,
        "schema": schema.model_dump(mode="json"),
        "params": params.model_dump(mode="json"),
        "trials": [trial.model_dump(mode="json") for trial in trials],
        "cutoff": training_cutoff.isoformat(),
        "rows": sorted(row.snapshot.snapshot_sha256 for row in rows),
        "seed": config.seed,
        "library": xgb.__version__,
        "booster": booster_sha,
    }
    sha = digest(body)
    return TabularArtifact(
        model_id=stable_id("tabular-model", sha),
        name=config.name,
        version=config.version,
        library_version=xgb.__version__,
        feature_schema=schema,
        params=params,
        trials=trials,
        training_cutoff=training_cutoff,
        training_matches=len(matches),
        training_rows=len(rows),
        seed=config.seed,
        booster_json=raw.decode(),
        booster_sha256=booster_sha,
        artifact_sha256=sha,
    )


def load_booster(artifact: TabularArtifact) -> xgb.Booster:
    raw = artifact.booster_json.encode()
    if hashlib.sha256(raw).hexdigest() != artifact.booster_sha256:
        raise ValueError("The booster bytes do not match their hash")
    booster = xgb.Booster()
    booster.load_model(bytearray(raw))
    return booster


def predict_tabular(
    booster: xgb.Booster,
    artifact: TabularArtifact,
    snapshot: FeatureSnapshot,
    market: MarketSource | None = None,
) -> Decimal:
    """Probability that canonical player one wins, for one snapshot."""
    schema = artifact.feature_schema
    if (snapshot.feature_set, snapshot.feature_set_sha256) != (
        schema.feature_set,
        schema.feature_set_sha256,
    ):
        raise ValueError("Snapshot feature set differs from the model's feature set")
    if schema.market_input != (market is not None):
        raise ValueError("The market input must match the model's schema")
    (probability,) = _symmetric(booster, schema, [_values(snapshot, market)])
    return _decimal(probability, PROBABILITY_QUANTUM)
