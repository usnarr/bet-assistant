"""F18 and F15.6 audited traces. A trace holds codes, IDs, hashes and counts only.

A trace never holds evidence text, evidence values, tool results, prompts or model prose.
So it cannot leak a secret or a restricted source value. Every tool attempt is recorded,
also a denied one. A denied unauthorized attempt stays an agent-behaviour failure.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from tennis_engine.common.contracts import Contract, ExactDecimal, Identifier, Timestamp

from .contracts import AgentRole, ModelRef

Code = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,96}$")]
Detail = dict[Code, Code | int | tuple[Code, ...]]


# COMPLETED, ABSTAINED and REVIEW_REQUIRED carry a verified output. REJECTED means that
# the output failed verification. The other states are typed incomplete outcomes. Without
# a verified output, the caller uses the deterministic fallback.
Status = Literal[
    "COMPLETED",
    "ABSTAINED",
    "REVIEW_REQUIRED",
    "REJECTED",
    "TIMEOUT",
    "BUDGET_EXHAUSTED",
    "EXPIRED",
    "DISABLED",
    "MODEL_UNAVAILABLE",
]


class TraceEvent(Contract):
    sequence: Annotated[int, Field(ge=0)]
    at: Timestamp
    kind: Literal["RUN_START", "MODEL_CALL", "TOOL_CALL", "VERIFICATION", "RUN_END"]
    outcome: Code
    detail: Detail = Field(default_factory=dict)


class AgentTrace(Contract):
    schema_version: Literal["1.0"] = "1.0"
    trace_id: UUID
    run_id: UUID
    role: AgentRole
    role_version: Identifier
    prompt_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    tool_schema_version: Identifier
    model: ModelRef
    context_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    as_of: Timestamp
    started_at: Timestamp
    finished_at: Timestamp
    status: Status
    events: tuple[TraceEvent, ...]
    tool_attempts: Annotated[int, Field(ge=0)]
    model_calls: Annotated[int, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    estimated_cost: Annotated[ExactDecimal, Field(ge=0)]
    # Unauthorized, forbidden or out-of-scope tool attempts, also when denied.
    critical_attempts: tuple[Code, ...] = ()
    # Verification finding codes of the final output.
    findings: tuple[Code, ...] = ()
    confirmed_proposals: tuple[str, ...] = ()
    fallback_used: bool


class TraceRecorder:
    """Collects events during one run. Only safe codes can be added."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
        self.tool_attempts = 0
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = Decimal("0")
        self.critical: list[str] = []
        self.proposals: list[str] = []

    def add(
        self,
        at: datetime,
        kind: Literal["RUN_START", "MODEL_CALL", "TOOL_CALL", "VERIFICATION", "RUN_END"],
        outcome: str,
        **detail: str | int | tuple[str, ...],
    ) -> None:
        self.events.append(
            TraceEvent.model_validate(
                {
                    "sequence": len(self.events),
                    "at": at,
                    "kind": kind,
                    "outcome": outcome,
                    "detail": detail,
                }
            )
        )
