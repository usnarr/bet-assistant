"""Immutable inputs. Approval is explicit; all intervals are [start, end)."""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware datetime required")
    return value.astimezone(UTC)


def exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("Use a decimal string or Decimal, never a float")
    return value


Timestamp = Annotated[datetime, AfterValidator(utc)]
Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.:-]*$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Amount = Annotated[
    Decimal, BeforeValidator(exact_decimal), Field(ge=0, allow_inf_nan=False, decimal_places=2)
]
Fraction = Annotated[
    Decimal, BeforeValidator(exact_decimal), Field(ge=0, le=1, allow_inf_nan=False)
]
Count = Annotated[int, Field(ge=0, strict=True)]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class Lifecycle(StrEnum):
    DRAFT = "DRAFT"
    PROTOTYPE_APPROVED = "PROTOTYPE_APPROVED"
    PRODUCTION_APPROVED = "PRODUCTION_APPROVED"
    SUSPENDED = "SUSPENDED"


class Purpose(StrEnum):
    PROTOTYPE = "prototype"
    PRODUCTION = "production"
    REDISTRIBUTION = "redistribution"


class Role(StrEnum):
    POLICY_REVIEWER = "policy_reviewer"
    OPERATOR = "operator"
    DASHBOARD = "dashboard"
    AGENT = "agent"


class Principal(Contract):
    """Supplied by a trusted host, never deserialized from a request body."""

    identity: Text
    role: Role


class EvidenceRef(Contract):
    document_id: Identifier
    sha256: Digest


class Interval(Contract):
    effective_from: Timestamp
    effective_until: Timestamp

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.effective_until <= self.effective_from:
            raise ValueError("An effective interval must be nonempty")
        return self

    def contains(self, when: datetime) -> bool:
        return self.effective_from <= utc(when) < self.effective_until


class ReviewedPolicy(Interval):
    version: Identifier
    owner: Text
    reviewer: Text | None = None
    reviewed_at: Timestamp | None = None
    review_due_at: Timestamp | None = None
    evidence: tuple[EvidenceRef, ...] = ()

    def require_review(self) -> None:
        if not self.reviewer or not self.reviewed_at or not self.review_due_at or not self.evidence:
            raise ValueError("Approval requires reviewer, review dates and archived evidence")
        if self.review_due_at <= self.reviewed_at:
            raise ValueError("Review expiry must follow the review")
        if len({item.document_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("Evidence document IDs must be unique")


class Quota(Contract):
    requests: Annotated[int, Field(gt=0, strict=True)]
    window_seconds: Annotated[int, Field(gt=0, strict=True)]
    concurrency: Annotated[int, Field(gt=0, strict=True)]


class Retention(Contract):
    raw_days: Annotated[int, Field(gt=0, strict=True)]
    basis: Text
    on_expiry: Literal["delete_with_tombstone"] = "delete_with_tombstone"


class SourcePolicy(ReviewedPolicy):
    source_id: Identifier
    category: Literal["sports", "odds", "regulations", "rankings", "forecasts", "research"]
    access_method: Literal["rest", "websocket", "file", "browser", "manual", "undecided"]
    terms_reference: Text
    commercial_use: Literal["yes", "no", "unknown"] = "unknown"
    redistribution: Literal["yes", "no", "unknown"] = "unknown"
    allowed_purposes: tuple[Purpose, ...] = ()
    quota: Quota | None = None
    retention: Retention | None = None
    state: Lifecycle = Lifecycle.DRAFT
    kill_switch: bool = True

    @model_validator(mode="after")
    def approval_complete(self) -> Self:
        if self.state in (Lifecycle.PROTOTYPE_APPROVED, Lifecycle.PRODUCTION_APPROVED):
            self.require_review()
            if not self.quota or not self.retention or not self.allowed_purposes:
                raise ValueError("Approval requires quota, retention and allowed purposes")
            if self.access_method == "undecided":
                raise ValueError("Approval requires an access method")
            if self.state == Lifecycle.PROTOTYPE_APPROVED and self.allowed_purposes != (
                Purpose.PROTOTYPE,
            ):
                raise ValueError("Prototype approval permits only prototype use")
            if self.state == Lifecycle.PRODUCTION_APPROVED and self.commercial_use != "yes":
                raise ValueError("Production approval requires explicit commercial rights")
            if Purpose.REDISTRIBUTION in self.allowed_purposes and self.redistribution != "yes":
                raise ValueError("Redistribution rights must be explicit")
        return self


class PayoutPolicy(ReviewedPolicy):
    bookmaker: Identifier
    jurisdiction: Literal["PL"] = "PL"
    state: Literal["PENDING_REVIEW", "APPROVED", "SUSPENDED"] = "PENDING_REVIEW"
    # F06 resolves these reviewed rule IDs. There are deliberately no sample tax defaults.
    payout_rule_version: Identifier | None = None
    settlement_rule_version: Identifier | None = None
    promotion_handling: Literal["disabled", "reviewed_rules"] = "disabled"
    promotion_rule_version: Identifier | None = None

    @model_validator(mode="after")
    def approval_complete(self) -> Self:
        if self.state == "APPROVED":
            self.require_review()
            if not self.payout_rule_version or not self.settlement_rule_version:
                raise ValueError("Approved payout and settlement rule versions are required")
            if self.promotion_handling == "reviewed_rules" and not self.promotion_rule_version:
                raise ValueError("Promotions require a reviewed rule version")
        return self


class PeriodLimit(Contract):
    stake: Amount
    count: Count


class ResponsibleUsePolicy(ReviewedPolicy):
    account_scope: Identifier
    currency: Literal["PLN"] = "PLN"
    reset_timezone: Literal["Europe/Warsaw"] = "Europe/Warsaw"
    ledger_scope: Literal["virtual", "actual"]
    daily: PeriodLimit
    weekly: PeriodLimit
    monthly: PeriodLimit
    max_event_exposure: Amount
    max_open_exposure: Amount
    max_bankroll_fraction: Fraction
    drawdown_stop: Fraction
    cooling_off_until: Timestamp | None = None
    disable_recommendations: bool = True
    loss_chasing_allowed: Literal[False] = False
    state: Literal["PENDING_REVIEW", "APPROVED", "SUSPENDED"] = "PENDING_REVIEW"

    @model_validator(mode="after")
    def approval_complete(self) -> Self:
        if self.state == "APPROVED":
            self.require_review()
        # Also verifies timezone availability on Windows (tzdata is a dependency).
        ZoneInfo(self.reset_timezone)
        return self


class PayoutSchedule(Contract):
    bookmaker: Identifier
    policies: tuple[PayoutPolicy, ...]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        validate_schedule(self.policies)
        if any(item.bookmaker != self.bookmaker for item in self.policies):
            raise ValueError("Schedule bookmaker mismatch")
        return self


class ResponsibleUseSchedule(Contract):
    account_scope: Identifier
    policies: tuple[ResponsibleUsePolicy, ...]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        validate_schedule(self.policies)
        if any(item.account_scope != self.account_scope for item in self.policies):
            raise ValueError("Schedule account scope mismatch")
        return self


def validate_schedule(policies: tuple[ReviewedPolicy, ...]) -> None:
    if not policies or len({item.version for item in policies}) != len(policies):
        raise ValueError("A schedule requires unique policy versions")
    ordered = sorted(policies, key=lambda item: item.effective_from)
    if any(
        left.effective_until > right.effective_from
        for left, right in zip(ordered, ordered[1:], strict=False)
    ):
        raise ValueError("Policy effective intervals cannot overlap")


class Reason(StrEnum):
    ALLOWED = "ALLOWED"
    SOURCE_UNKNOWN = "SOURCE_UNKNOWN"
    SOURCE_DISABLED = "SOURCE_DISABLED"
    SOURCE_SUSPENDED = "SOURCE_SUSPENDED"
    SOURCE_NOT_APPROVED = "SOURCE_NOT_APPROVED"
    SOURCE_OUTSIDE_EFFECTIVE_INTERVAL = "SOURCE_OUTSIDE_EFFECTIVE_INTERVAL"
    PRODUCTION_APPROVAL_REQUIRED = "PRODUCTION_APPROVAL_REQUIRED"
    PURPOSE_NOT_ALLOWED = "PURPOSE_NOT_ALLOWED"
    POLICY_NOT_REVIEWED = "POLICY_NOT_REVIEWED"
    REVIEW_EXPIRED = "REVIEW_EXPIRED"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    DOCUMENT_REVIEW_REQUIRED = "DOCUMENT_REVIEW_REQUIRED"
    EVIDENCE_INTEGRITY_FAILURE = "EVIDENCE_INTEGRITY_FAILURE"
    PAYOUT_POLICY_MISSING = "PAYOUT_POLICY_MISSING"
    PAYOUT_POLICY_NOT_APPROVED = "PAYOUT_POLICY_NOT_APPROVED"
    PAYOUT_POLICY_OUTSIDE_EFFECTIVE_INTERVAL = "PAYOUT_POLICY_OUTSIDE_EFFECTIVE_INTERVAL"
    GLOBAL_DISABLE = "GLOBAL_DISABLE"
    RESPONSIBLE_USE_POLICY_MISSING = "RESPONSIBLE_USE_POLICY_MISSING"
    RESPONSIBLE_USE_POLICY_NOT_APPROVED = "RESPONSIBLE_USE_POLICY_NOT_APPROVED"
    RESPONSIBLE_USE_POLICY_OUTSIDE_EFFECTIVE_INTERVAL = (
        "RESPONSIBLE_USE_POLICY_OUTSIDE_EFFECTIVE_INTERVAL"
    )
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    COOLING_OFF = "COOLING_OFF"
    SOURCE_LINEAGE_MISSING = "SOURCE_LINEAGE_MISSING"


class Decision(Contract):
    allowed: bool
    reason: Reason = Reason.ALLOWED
    version: str | None = None
    revision: int | None = None
    recommendation: Literal["NO_BET"] | None = None

    @classmethod
    def deny(cls, reason: str, version: str | None = None, revision: int | None = None) -> Self:
        return cls(
            allowed=False,
            reason=Reason(reason),
            version=version,
            revision=revision,
            recommendation="NO_BET",
        )
