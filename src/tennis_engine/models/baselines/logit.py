"""One-coefficient logistic regression in ``Decimal`` (deterministic across rebuilds).

There is no intercept: canonical player order is arbitrary, so ``p(x)`` must satisfy
``p(-x) = 1 - p(x)``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

PRECISION = 40
ONE = Decimal(1)


def sigmoid(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        if value >= 0:
            return ONE / (ONE + (-value).exp())
        exp = value.exp()
        return exp / (ONE + exp)


def log_loss_term(probability: Decimal, outcome: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return -(probability if outcome else ONE - probability).ln()


@dataclass(frozen=True)
class Fit:
    coefficient: Decimal
    iterations: int
    converged: bool


def fit(
    xs: Sequence[Decimal],
    ys: Sequence[int],
    *,
    l2: Decimal,
    initial: Decimal = Decimal(0),
    tolerance: Decimal = Decimal("1e-15"),
    max_iterations: int = 100,
) -> Fit:
    """Newton-Raphson for the ridge-penalized log likelihood of ``sigmoid(beta * x)``."""
    if len(xs) != len(ys) or not xs:
        raise ValueError("Fitting requires matched, non-empty inputs")
    if any(item not in (0, 1) for item in ys):
        raise ValueError("Outcomes must be 0 or 1")
    beta = initial
    with localcontext() as ctx:
        ctx.prec = PRECISION
        for iteration in range(1, max_iterations + 1):
            gradient = l2 * beta
            hessian = l2
            for x, y in zip(xs, ys, strict=True):
                p = sigmoid(beta * x)
                gradient += (p - y) * x
                hessian += p * (ONE - p) * x * x
            if hessian <= 0:
                return Fit(beta, iteration, False)
            step = gradient / hessian
            beta -= step
            if abs(step) < tolerance:
                return Fit(+beta, iteration, True)
    return Fit(beta, max_iterations, False)
