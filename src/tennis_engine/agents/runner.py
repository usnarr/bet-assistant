"""F18.6 bounded agent runs with typed outcomes and deterministic fallback.

A run stops at the first of: kill switch, context expiry, deadline, tool budget, model
call budget, token budget, cost budget or retry limit. It then returns a typed outcome
without output. The caller uses the deterministic path. A run never extends a quote or
decision expiry: a context past `expires_at` gives `EXPIRED`, also after a valid output.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import ValidationError

from tennis_engine.common.clock import Clock, require_aware
from tennis_engine.common.ids import new_id, stable_id

from .contracts import (
    AgentOutput,
    AgentRunContext,
    AuthorizedScope,
    Budget,
    EvidenceRecord,
    ModelRef,
)
from .gateway import ToolGateway
from .model import LanguageModel, ModelRequest, ModelTimeout, ModelTurn, ToolResponse
from .proposals import AgentRecordStore
from .roles import RoleSpec
from .switch import AgentSwitch
from .tools import TOOL_SCHEMA_VERSION, ToolBackend
from .trace import AgentTrace, Status, TraceRecorder
from .verify import BLOCKING, Finding, Severity, verify_output

USABLE = frozenset({"COMPLETED", "ABSTAINED", "REVIEW_REQUIRED"})
OUTPUT_SCHEMA = AgentOutput.model_json_schema()

ExtraVerifier = Callable[[AgentOutput, Mapping[str, EvidenceRecord]], list[Finding]]


@dataclass(frozen=True)
class AgentRunResult:
    status: Status
    output: AgentOutput | None
    findings: tuple[Finding, ...]
    trace: AgentTrace
    trace_recorded: bool
    # The first output the model returned, kept for evaluation even when rejected.
    raw_output: AgentOutput | None = None

    @property
    def usable(self) -> bool:
        return self.status in USABLE and self.output is not None and self.trace_recorded


def _cost(context: AgentRunContext, input_tokens: int, output_tokens: int) -> Decimal:
    model = context.model
    return (
        Decimal(input_tokens) * model.input_cost_per_1k
        + Decimal(output_tokens) * model.output_cost_per_1k
    ) / Decimal(1000)


class _Run:
    def __init__(
        self,
        context: AgentRunContext,
        spec: RoleSpec,
        model: LanguageModel,
        gateway: ToolGateway,
        clock: Clock,
    ) -> None:
        self.context = context
        self.spec = spec
        self.model = model
        self.gateway = gateway
        self.clock = clock
        self.recorder = gateway.recorder
        self.transcript: list[ToolResponse] = []

    def now(self) -> datetime:
        return require_aware(self.clock.now())

    def expired(self) -> bool:
        expiry = self.context.expires_at
        return expiry is not None and self.now() >= expiry

    def request(self) -> ModelRequest:
        budget = self.context.budget
        used = self.recorder.input_tokens + self.recorder.output_tokens
        return ModelRequest(
            role=self.context.role,
            system_prompt=self.spec.prompt,
            task=self.context.task,
            as_of=self.context.as_of,
            subject_ids=self.context.authorized_scope.subject_ids,
            allowed_tools=tuple(sorted(self.spec.allowed_tools)),
            output_schema=OUTPUT_SCHEMA,
            evidence=tuple(self.gateway.seen.values()),
            tool_results=tuple(self.transcript),
            remaining_tool_calls=max(0, budget.max_tool_calls - self.recorder.tool_attempts),
            remaining_tokens=max(0, budget.max_tokens - used),
        )

    def call_model(self) -> ModelTurn | Status:
        budget = self.context.budget
        timed_out = False
        for attempt in range(budget.max_retries + 1):
            self.recorder.model_calls += 1
            try:
                turn = self.model.complete(self.request())
            except ModelTimeout:
                timed_out = True
                self.recorder.add(self.now(), "MODEL_CALL", "TIMEOUT", attempt=attempt)
                continue
            except Exception as error:  # noqa: BLE001 - any provider failure is transient here
                self.recorder.add(
                    self.now(), "MODEL_CALL", "ERROR", attempt=attempt, error=type(error).__name__
                )
                continue
            self.recorder.input_tokens += turn.input_tokens
            self.recorder.output_tokens += turn.output_tokens
            self.recorder.cost = _cost(
                self.context, self.recorder.input_tokens, self.recorder.output_tokens
            )
            self.recorder.add(
                self.now(),
                "MODEL_CALL",
                "OK",
                attempt=attempt,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                tool_calls=len(turn.tool_calls),
                final=int(turn.final is not None),
            )
            return turn
        return "TIMEOUT" if timed_out else "MODEL_UNAVAILABLE"

    def over_budget(self) -> Status | None:
        budget = self.context.budget
        if self.recorder.input_tokens + self.recorder.output_tokens > budget.max_tokens:
            return "BUDGET_EXHAUSTED"
        if self.recorder.cost > budget.max_cost:
            return "BUDGET_EXHAUSTED"
        if self.now() >= self.context.deadline():
            return "TIMEOUT"
        return None


def new_context(
    spec: RoleSpec,
    *,
    task: str,
    subject_ids: tuple[str, ...],
    as_of: datetime,
    started_at: datetime,
    model: ModelRef,
    budget: Budget,
    evidence: tuple[EvidenceRecord, ...] = (),
    expires_at: datetime | None = None,
    run_id: UUID | None = None,
) -> AgentRunContext:
    run = run_id or new_id()
    return AgentRunContext(
        run_id=run,
        trace_id=stable_id("agent-trace", str(run)),
        role=spec.role,
        task=task,
        authorized_scope=AuthorizedScope(subject_ids=subject_ids),
        as_of=as_of,
        started_at=started_at,
        expires_at=expires_at,
        evidence=evidence,
        model=model,
        role_version=spec.version,
        prompt_sha256=spec.prompt_sha256,
        tool_schema_version=TOOL_SCHEMA_VERSION,
        budget=budget,
    )


def run_agent(
    context: AgentRunContext,
    *,
    spec: RoleSpec,
    model: LanguageModel,
    backend: ToolBackend,
    store: AgentRecordStore,
    switch: AgentSwitch,
    clock: Clock,
    extra_verifier: ExtraVerifier | None = None,
) -> AgentRunResult:
    if spec.role != context.role or spec.prompt_sha256 != context.prompt_sha256:
        raise ValueError("The context was built for another role specification")
    recorder = TraceRecorder()
    gateway = ToolGateway(
        context=context,
        spec=spec,
        backend=backend,
        store=store,
        switch=switch,
        clock=clock,
        recorder=recorder,
    )
    run = _Run(context, spec, model, gateway, clock)
    recorder.add(run.now(), "RUN_START", "STARTED", evidence=len(gateway.seen))
    output: AgentOutput | None = None
    raw: AgentOutput | None = None
    findings: list[Finding] = []
    status: Status = "BUDGET_EXHAUSTED"

    disabled = gateway._switch_reason(run.now())
    if disabled is not None:
        status = "DISABLED"
        recorder.add(run.now(), "RUN_END", "DISABLED", reason=disabled)
    elif run.expired():
        status = "EXPIRED"
    else:
        for _ in range(context.budget.max_model_calls):
            if run.now() >= context.deadline():
                status = "TIMEOUT"
                break
            turn = run.call_model()
            if isinstance(turn, str):
                status = turn
                break
            limit = run.over_budget()
            if limit is not None:
                status = limit
                break
            if turn.final is not None:
                try:
                    raw = AgentOutput.model_validate(turn.final)
                except ValidationError:
                    findings.append(Finding("STRUCTURED_INVALID", Severity.MAJOR))
                    status = "REJECTED"
                    break
                findings.extend(
                    verify_output(
                        raw,
                        context=context,
                        spec=spec,
                        seen=gateway.seen,
                        confirmed_proposals=gateway.confirmed,
                    )
                )
                if extra_verifier is not None:
                    findings.extend(extra_verifier(raw, gateway.seen))
                if recorder.critical:
                    findings.append(Finding("UNAUTHORIZED_ATTEMPT", Severity.CRITICAL))
                if run.expired():
                    status = "EXPIRED"
                elif any(item.severity in BLOCKING for item in findings):
                    status = "REJECTED"
                else:
                    status = raw.status
                    output = raw
                break
            for request in turn.tool_calls:
                run.transcript.append(gateway.call(request))
                if gateway.stop is not None:
                    break
            if gateway.stop is not None:
                status = gateway.stop
                break
    codes = tuple(sorted({item.code for item in findings}))
    recorder.add(run.now(), "VERIFICATION", "PASS" if output else "NO_OUTPUT", findings=codes)
    recorder.add(run.now(), "RUN_END", status)
    trace = AgentTrace(
        trace_id=context.trace_id,
        run_id=context.run_id,
        role=context.role,
        role_version=spec.version,
        prompt_sha256=spec.prompt_sha256,
        tool_schema_version=context.tool_schema_version,
        model=context.model,
        context_sha256=context.digest,
        as_of=context.as_of,
        started_at=context.started_at,
        finished_at=run.now(),
        status=status,
        events=tuple(recorder.events),
        tool_attempts=recorder.tool_attempts,
        model_calls=recorder.model_calls,
        input_tokens=recorder.input_tokens,
        output_tokens=recorder.output_tokens,
        estimated_cost=recorder.cost,
        critical_attempts=tuple(recorder.critical),
        findings=codes,
        confirmed_proposals=tuple(gateway.confirmed),
        fallback_used=output is None,
    )
    try:
        store.record_trace(trace)
        recorded = True
    except Exception:  # noqa: BLE001 - an output without an audited trace is not used
        recorded = False
    return AgentRunResult(
        status=status,
        output=output if recorded else None,
        findings=tuple(findings),
        trace=trace,
        trace_recorded=recorded,
        raw_output=raw,
    )
