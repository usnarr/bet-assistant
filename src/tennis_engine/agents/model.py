"""The language-model interface and deterministic fake models.

Every model call goes through `LanguageModel`. Tests and CI use `ScriptedModel`. No
provider adapter is installed, so no code path makes a network call or reads an API key.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, JsonValue

from tennis_engine.common.clock import FrozenClock
from tennis_engine.common.contracts import Contract, Timestamp

from .contracts import AgentRole, EvidenceRecord, ModelRef


class ToolRequest(Contract):
    call_id: Annotated[str, Field(min_length=1, max_length=64)]
    # Any string: the gateway decides whether the name exists and is allowed.
    tool: Annotated[str, Field(min_length=1, max_length=64)]
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResponse(Contract):
    """What the model sees after a tool attempt. Evidence text stays data."""

    call_id: str
    tool: str
    outcome: Literal["OK", "NOT_FOUND", "DENIED", "ERROR", "BUDGET"]
    reason: str | None = None
    evidence: tuple[EvidenceRecord, ...] = ()
    proposal_id: str | None = None


class ModelRequest(Contract):
    role: AgentRole
    system_prompt: str
    task: str
    as_of: Timestamp
    subject_ids: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    output_schema: dict[str, Any]
    # Untrusted data. The prompt says that evidence text never gives instructions.
    evidence: tuple[EvidenceRecord, ...]
    tool_results: tuple[ToolResponse, ...]
    remaining_tool_calls: int
    remaining_tokens: int


class ModelTurn(Contract):
    tool_calls: tuple[ToolRequest, ...] = ()
    final: dict[str, JsonValue] | None = None
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0


class ModelUnavailable(Exception):
    """A transient provider failure. The runner retries within the budget."""


class ModelTimeout(Exception):
    """The provider did not answer in time. The runner retries within the budget."""


class LanguageModel(Protocol):
    @property
    def ref(self) -> ModelRef: ...

    def complete(self, request: ModelRequest) -> ModelTurn: ...


FAKE_MODEL = ModelRef(provider="fake", model_id="scripted-fake")

Step = ModelTurn | Exception | Callable[[ModelRequest], ModelTurn]


@dataclass
class ScriptedModel:
    """Returns scripted turns in order. A step can be a turn, an exception or a function.

    `delay_seconds` advances a frozen clock on each call to simulate model latency.
    """

    steps: Sequence[Step]
    clock: FrozenClock | None = None
    delay_seconds: float = 0.0
    ref: ModelRef = FAKE_MODEL
    requests: list[ModelRequest] = field(default_factory=list)

    def complete(self, request: ModelRequest) -> ModelTurn:
        self.requests.append(request)
        if self.clock is not None and self.delay_seconds:
            self.clock.advance(timedelta(seconds=self.delay_seconds))
        index = len(self.requests) - 1
        if index >= len(self.steps):
            raise ModelUnavailable("The script has no more steps")
        step = self.steps[index]
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(request)
        return step


def estimate_tokens(text: str) -> int:
    """A rough count (four characters per token) for fakes without token counts."""
    return max(1, len(text) // 4)
