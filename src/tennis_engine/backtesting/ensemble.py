"""F11 tabular model and stacker as walk-forward candidates (F11.1 to F11.4, F09.8).

``TabularCandidate`` refits the XGBoost model in every outer fold; its tuning uses inner
folds of that fold's training rows only, so tuning is nested. ``StackedCandidate`` builds
chronological out-of-fold predictions: in each inner fold every component is fitted on rows
before the inner cutoff and predicts the later validation matches. The stacker is fitted on
those rows only, then every component is refitted on the whole training period.

For F09.8, run a ``TabularCandidate`` with and without ``market`` in one harness run and
compare them on matched rows.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import xgboost as xgb

from tennis_engine.features.contracts import FeatureSnapshot, digest
from tennis_engine.models.baselines.baseline import TrainingRow
from tennis_engine.models.baselines.contracts import SupportStatus
from tennis_engine.models.tabular.booster import (
    MarketSource,
    TabularArtifact,
    TabularConfig,
    load_booster,
    predict_tabular,
    train_tabular,
)
from tennis_engine.models.tabular.folds import inner_folds
from tennis_engine.models.tabular.stacker import (
    OutOfFoldRow,
    StackerArtifact,
    fit_stacker,
    stack,
)

from .runner import Candidate, Fitted, Output


@dataclass(frozen=True)
class _FittedTabular:
    artifact: TabularArtifact
    booster: xgb.Booster
    market: MarketSource | None

    @property
    def version(self) -> str:
        return self.artifact.version

    @property
    def artifact_sha256(self) -> str:
        return self.artifact.artifact_sha256

    def predict(self, snapshot: FeatureSnapshot) -> Output:
        probability = predict_tabular(self.booster, self.artifact, snapshot, self.market)
        return Output(SupportStatus.SUPPORTED, (), probability)


@dataclass(frozen=True)
class TabularCandidate:
    config: TabularConfig = field(default_factory=TabularConfig)
    market: MarketSource | None = None

    @property
    def name(self) -> str:
        return self.config.name

    def fit(self, rows: Sequence[TrainingRow], training_cutoff: datetime) -> Fitted:
        artifact = train_tabular(
            rows, training_cutoff=training_cutoff, config=self.config, market=self.market
        )
        return _FittedTabular(artifact, load_booster(artifact), self.market)


@dataclass(frozen=True)
class _FittedStack:
    stacker: StackerArtifact
    components: tuple[Fitted, ...]
    names: tuple[str, ...]

    @property
    def version(self) -> str:
        return f"stack-{self.stacker.artifact_sha256[:12]}"

    @property
    def artifact_sha256(self) -> str:
        return digest(
            {
                "stacker": self.stacker.artifact_sha256,
                "components": [item.artifact_sha256 for item in self.components],
            }
        )

    def predict(self, snapshot: FeatureSnapshot) -> Output:
        probabilities: list[Decimal | None] = []
        missing = []
        for name, component in zip(self.names, self.components, strict=True):
            output = component.predict(snapshot)
            usable = output.support != SupportStatus.UNSUPPORTED
            probabilities.append(output.probability if usable else None)
            if not usable or output.probability is None:
                missing.append(f"component_missing:{name}")
        if len(missing) == len(self.components):
            return Output(SupportStatus.UNSUPPORTED, tuple(missing), None)
        probability = stack(self.stacker, probabilities)
        support = SupportStatus.SPARSE if missing else SupportStatus.SUPPORTED
        return Output(support, tuple(missing), probability)


@dataclass(frozen=True)
class StackedCandidate:
    components: tuple[Candidate, ...]
    name: str = "stacked-ensemble"
    inner: int = 3
    l2: Decimal = Decimal(1)

    def out_of_fold(self, rows: Sequence[TrainingRow]) -> tuple[list[OutOfFoldRow], int]:
        folds = inner_folds(rows, folds=self.inner)
        oof = []
        for fold in folds:
            fitted: list[Fitted | None] = []
            for component in self.components:
                try:
                    fitted.append(component.fit(fold.train, fold.cutoff))
                except ValueError:
                    # BLOCKED in this inner fold: the component counts as missing here,
                    # the same way a missing prediction counts at scoring time.
                    fitted.append(None)
            seen = {row.snapshot.match_id for row in fold.train}
            for row in fold.validate:
                outputs = [
                    item.predict(row.snapshot)
                    if item is not None
                    else Output(SupportStatus.UNSUPPORTED, ("fit_blocked",), None)
                    for item in fitted
                ]
                oof.append(
                    OutOfFoldRow(
                        match_id=row.snapshot.match_id,
                        as_of=row.snapshot.as_of,
                        outcome=1 if row.label.player_one_won else 0,
                        probabilities=tuple(
                            output.probability
                            if output.support != SupportStatus.UNSUPPORTED
                            else None
                            for output in outputs
                        ),
                        fitted_at=tuple(fold.cutoff for _ in fitted),
                        in_sample=tuple(row.snapshot.match_id in seen for _ in fitted),
                    )
                )
        return oof, len(folds)

    def fit(self, rows: Sequence[TrainingRow], training_cutoff: datetime) -> Fitted:
        names = tuple(component.name for component in self.components)
        if len(set(names)) != len(names) or not names:
            raise ValueError("A stack needs uniquely named components")
        oof, folds = self.out_of_fold(rows)
        stacker = fit_stacker(
            oof, components=names, training_cutoff=training_cutoff, folds=folds, l2=self.l2
        )
        final = tuple(component.fit(rows, training_cutoff) for component in self.components)
        return _FittedStack(stacker, final, names)
