"""F18.1 and F18.2 agent contracts: roles, evidence, budgets, run context and output.

Evidence text is untrusted data. It never carries instructions or authority. An agent
output is a proposal or a summary. It never changes a probability, a payout, a stake, a
risk limit, an identity or a policy.
"""

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, ExactDecimal, Identifier, Timestamp
from tennis_engine.contracts.domain import RecommendationStatus


class AgentRole(StrEnum):
    DATA_INTAKE = "data_intake_analyst"
    IDENTITY_REVIEW = "identity_review_assistant"
    RESEARCH = "research_feature_assistant"
    MODEL_ANALYSIS = "model_evaluation_analyst"
    VALUE_RISK = "value_risk_reviewer"
    EXPLANATION = "explanation_assistant"
    MONITORING = "monitoring_incident_assistant"

    @property
    def prefix(self) -> str:
        return ROLE_PREFIX[self]


ROLE_PREFIX: dict[AgentRole, str] = {
    AgentRole.DATA_INTAKE: "AG-DI",
    AgentRole.IDENTITY_REVIEW: "AG-ID",
    AgentRole.RESEARCH: "AG-RF",
    AgentRole.MODEL_ANALYSIS: "AG-MA",
    AgentRole.VALUE_RISK: "AG-VR",
    AgentRole.EXPLANATION: "AG-EX",
    AgentRole.MONITORING: "AG-MO",
}


class AccessClass(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    # Values are withheld from agents: the source does not permit this use.
    RESTRICTED = "RESTRICTED"
    # Never shown to an agent. A secret in an output is exfiltration.
    SECRET = "SECRET"


Value = Annotated[str, Field(min_length=1, max_length=200)]
ShortText = Annotated[str, Field(min_length=1, max_length=2000)]


def canonical_json(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256(data: object) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()


def as_decimal(value: str) -> Decimal | None:
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


class EvidenceRecord(Contract):
    """One sanitized record. `values` are canonical; `text` is untrusted prose."""

    evidence_id: Identifier
    kind: Identifier
    source_id: Identifier | None = None
    available_at: Timestamp
    access_class: AccessClass = AccessClass.INTERNAL
    values: dict[Identifier, Value] = Field(default_factory=dict)
    text: Annotated[str, Field(max_length=4000)] | None = None
    withheld: bool = False

    @property
    def digest(self) -> str:
        return sha256(self.model_dump(mode="json"))

    def for_agent(self) -> "EvidenceRecord":
        """The view an agent may see. Restricted values and text are removed."""
        if self.access_class == AccessClass.RESTRICTED and not self.withheld:
            return self.model_copy(update={"values": {}, "text": None, "withheld": True})
        return self


class Budget(Contract):
    """Resource caps. Tool attempts include failures, retries and duplicates."""

    max_tool_calls: Annotated[int, Field(ge=0, le=50)]
    deadline_seconds: Annotated[int, Field(ge=1, le=600)]
    max_model_calls: Annotated[int, Field(ge=1, le=20)] = 4
    max_retries: Annotated[int, Field(ge=0, le=5)] = 1
    max_tokens: Annotated[int, Field(ge=1, le=2_000_000)]
    max_cost: Annotated[ExactDecimal, Field(ge=0)]


class ModelRef(Contract):
    provider: Identifier
    model_id: Identifier
    # Cost per 1000 tokens. Estimates only; billing data is the true cost.
    input_cost_per_1k: Annotated[ExactDecimal, Field(ge=0)] = Decimal("0")
    output_cost_per_1k: Annotated[ExactDecimal, Field(ge=0)] = Decimal("0")


class AuthorizedScope(Contract):
    """The subject IDs that tools may read or propose on. Only shadow mode exists."""

    subject_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    mode: Literal["shadow"] = "shadow"


class AgentRunContext(Contract):
    """F18.2 everything one run may use. `as_of` is the evidence cutoff."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    trace_id: UUID
    role: AgentRole
    task: ShortText
    authorized_scope: AuthorizedScope
    as_of: Timestamp
    started_at: Timestamp
    # A quote or decision expiry. A run past it cannot produce a current result.
    expires_at: Timestamp | None = None
    evidence: tuple[EvidenceRecord, ...] = ()
    model: ModelRef
    role_version: Identifier
    prompt_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    tool_schema_version: Identifier
    budget: Budget

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.as_of > self.started_at:
            raise ValueError("The cutoff cannot be later than the run start")
        ids = [item.evidence_id for item in self.evidence]
        if len(set(ids)) != len(ids):
            raise ValueError("Evidence IDs must be unique")
        return self

    @property
    def digest(self) -> str:
        return sha256(self.model_dump(mode="json"))

    def visible(self, record: EvidenceRecord) -> bool:
        return record.access_class != AccessClass.SECRET and record.available_at <= self.as_of

    def visible_evidence(self) -> tuple[EvidenceRecord, ...]:
        return tuple(item.for_agent() for item in self.evidence if self.visible(item))

    def deadline(self) -> datetime:
        return self.started_at + timedelta(seconds=self.budget.deadline_seconds)


class Claim(Contract):
    """One factual statement. Each quoted value must equal a cited record value."""

    text: Annotated[str, Field(min_length=1, max_length=600)]
    evidence_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=20)
    values: dict[Identifier, Value] = Field(default_factory=dict)


class AgentOutput(Contract):
    """F18.1 structured output shared by every role."""

    status: Literal["COMPLETED", "ABSTAINED", "REVIEW_REQUIRED"]
    summary: Annotated[str, Field(min_length=1, max_length=1500)]
    claims: tuple[Claim, ...] = Field(default=(), max_length=40)
    decision: RecommendationStatus | None = None
    recommended_stake: ExactDecimal | None = None
    reason_codes: tuple[Identifier | str, ...] = Field(default=(), max_length=40)
    proposal_ids: tuple[str, ...] = Field(default=(), max_length=20)
    abstention_reason: Annotated[str, Field(max_length=300)] | None = None

    @model_validator(mode="after")
    def abstention_has_reason(self) -> Self:
        if self.status != "COMPLETED" and not self.abstention_reason:
            raise ValueError("An abstention or review request needs a reason")
        return self
