"""Strict cross-feature value contracts."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from .clock import require_aware


def reject_float(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("Use a decimal string or Decimal, never a float")
    return value


def two_decimal_places(value: Decimal) -> Decimal:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        raise ValueError("Money supports at most two decimal places")
    return value


Timestamp = Annotated[datetime, AfterValidator(require_aware)]
Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.:-]*$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ExactDecimal = Annotated[Decimal, BeforeValidator(reject_float), Field(allow_inf_nan=False)]
Amount = Annotated[
    Decimal,
    BeforeValidator(reject_float),
    Field(allow_inf_nan=False),
    AfterValidator(two_decimal_places),
]
Probability = Annotated[
    Decimal, BeforeValidator(reject_float), Field(ge=0, le=1, allow_inf_nan=False)
]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class Currency(StrEnum):
    PLN = "PLN"


class ReasonCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    UNSUPPORTED_SCOPE = "UNSUPPORTED_SCOPE"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    SCHEMA_INCOMPATIBLE = "SCHEMA_INCOMPATIBLE"


class Money(Contract):
    amount: Amount
    currency: Currency = Currency.PLN


class VersionRef(Contract):
    component: Identifier
    version: Identifier
    sha256: Digest
