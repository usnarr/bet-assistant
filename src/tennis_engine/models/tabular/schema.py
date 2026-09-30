"""F11.1 feature schema, missingness flags and player-swap encoding for the tabular model.

- Numbers (``Decimal``, ``int``, ``bool``) become numeric columns. A missing number is NaN,
  which XGBoost routes by a learned default branch.
- A feature missing in any training row also gets a ``missing:<name>`` flag column.
- Text codes (``match.tour`` and similar) become one indicator column per level seen in
  training. An unseen level sets no indicator.
- The schema comes from training rows only. A column absent at prediction time is NaN.
- ``swap_values`` gives the reversed orientation: ``p1``/``p2`` exchange, ``diff.*``
  changes sign. The optional market input is ``diff.market_logit``, so it changes sign too.
"""

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal

from tennis_engine.common.contracts import Contract, Digest, Identifier
from tennis_engine.contracts.domain import FeatureValue

MARKET_FEATURE = "diff.market_logit"
PREFIXES = ("p1.", "p2.", "diff.", "match.")


class TabularSchema(Contract):
    feature_set: Identifier
    feature_set_sha256: Digest
    numeric: tuple[str, ...]
    flagged: tuple[str, ...]  # Numeric features with a missing flag column.
    levels: dict[str, tuple[str, ...]]  # Text feature -> levels seen in training.
    market_input: bool

    @property
    def columns(self) -> tuple[str, ...]:
        return (
            *self.numeric,
            *(f"missing:{name}" for name in self.flagged),
            *(f"{name}={level}" for name, values in self.levels.items() for level in values),
        )


def _usable(name: str) -> bool:
    return name.startswith(PREFIXES)


def build_schema(
    rows: Sequence[Mapping[str, FeatureValue]],
    *,
    feature_set: str,
    feature_set_sha256: str,
    market_input: bool,
) -> TabularSchema:
    """Schema from training values in both orientations, so it is swap-closed."""
    numeric: set[str] = set()
    text: dict[str, set[str]] = {}
    seen: dict[str, int] = {}
    for values in rows:
        for name, value in values.items():
            if not _usable(name):
                continue
            seen[name] = seen.get(name, 0) + 1
            if isinstance(value, str):
                text.setdefault(name, set()).add(value)
            elif value is not None:
                numeric.add(name)
    if market_input:
        numeric.add(MARKET_FEATURE)
    names = sorted(numeric)
    flagged = [
        name
        for name in names
        if name == MARKET_FEATURE
        or seen.get(name, 0) < len(rows)
        or any(values.get(name) is None for values in rows)
    ]
    return TabularSchema(
        feature_set=feature_set,
        feature_set_sha256=feature_set_sha256,
        numeric=tuple(names),
        flagged=tuple(flagged),
        levels={name: tuple(sorted(levels)) for name, levels in sorted(text.items())},
        market_input=market_input,
    )


def encode(values: Mapping[str, FeatureValue], schema: TabularSchema) -> list[float]:
    row: list[float] = []
    for name in schema.numeric:
        value = values.get(name)
        row.append(math.nan if value is None or isinstance(value, str) else float(value))
    for name in schema.flagged:
        value = values.get(name)
        row.append(1.0 if value is None or isinstance(value, str) else 0.0)
    for name, levels in schema.levels.items():
        value = values.get(name)
        row.extend(1.0 if value == level else 0.0 for level in levels)
    return row


def market_logit(probability: Decimal | None) -> Decimal | None:
    """Market input for player one, as a log-odds difference."""
    if probability is None or not 0 < probability < 1:
        return None
    return (probability / (1 - probability)).ln()
