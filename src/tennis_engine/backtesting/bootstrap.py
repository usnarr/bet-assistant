"""Paired block bootstrap by tournament week (F13.8).

Items are ``(block, numerator, denominator)``. The statistic is
``sum(numerator) / sum(denominator)``: a mean when every denominator is one, and ROI when
they are profit and stake. Paired comparisons put both models' values for the same row in
one item, so resampling keeps the pairing and the within-week correlation.
"""

import random
from collections.abc import Sequence
from decimal import Decimal, localcontext
from math import ceil, floor
from typing import Annotated, Literal

from pydantic import Field

from tennis_engine.common.contracts import Contract, Identifier

QUANTUM = Decimal("1e-9")
BLOCK_RULE = "iso-week of the snapshot cutoff (UTC); whole weeks drawn with replacement"


class BootstrapInterval(Contract):
    statistic: Identifier
    method: Literal["PAIRED_WEEK_BLOCK_BOOTSTRAP"] = "PAIRED_WEEK_BLOCK_BOOTSTRAP"
    block_rule: str = BLOCK_RULE
    estimate: Decimal
    lower: Decimal | None
    upper: Decimal | None
    level: Annotated[Decimal, Field(gt=0, lt=1)]
    draws: Annotated[int, Field(ge=0, strict=True)]
    usable_draws: Annotated[int, Field(ge=0, strict=True)]
    seed: int
    blocks: Annotated[int, Field(ge=0, strict=True)]
    rows: Annotated[int, Field(ge=0, strict=True)]


def block_bootstrap(
    items: Sequence[tuple[str, Decimal, Decimal]],
    *,
    statistic: str,
    draws: int,
    seed: int,
    level: Decimal,
) -> BootstrapInterval:
    """Percentile interval. Fewer than two blocks gives no interval (``None`` bounds)."""
    sums: dict[str, tuple[Decimal, Decimal]] = {}
    for block, numerator, denominator in items:
        top, bottom = sums.get(block, (Decimal(0), Decimal(0)))
        sums[block] = (top + numerator, bottom + denominator)
    keys = sorted(sums)
    with localcontext() as ctx:
        ctx.prec = 34
        total = sum((value[1] for value in sums.values()), Decimal(0))
        if total == 0:
            raise ValueError("The statistic is undefined: the denominator sum is zero")
        estimate = sum((value[0] for value in sums.values()), Decimal(0)) / total
        values: list[Decimal] = []
        if len(keys) >= 2:
            generator = random.Random(seed)
            for _ in range(draws):
                top = bottom = Decimal(0)
                for _ in keys:
                    chosen = sums[keys[generator.randrange(len(keys))]]
                    top += chosen[0]
                    bottom += chosen[1]
                if bottom != 0:
                    values.append(top / bottom)
    lower = upper = None
    if len(values) >= 2:
        values.sort()
        tail = (Decimal(1) - level) / 2
        lower = values[floor(tail * (len(values) - 1))].quantize(QUANTUM)
        upper = values[ceil((Decimal(1) - tail) * (len(values) - 1))].quantize(QUANTUM)
    return BootstrapInterval(
        statistic=statistic,
        estimate=estimate.quantize(QUANTUM),
        lower=lower,
        upper=upper,
        level=level,
        draws=draws,
        usable_draws=len(values),
        seed=seed,
        blocks=len(keys),
        rows=len(items),
    )
