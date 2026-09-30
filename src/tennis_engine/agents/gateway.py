"""F18.3 and F15.6 server-side tool gateway.

Each tool attempt passes these checks in order: tool budget, deadline, kill switch, tool
name and role allowlist, argument schema, authorized scope and cited evidence. Only then
does the gateway call a deterministic read service or write an idempotent proposal.

The gateway records every attempt in the trace, also a denied one. A forbidden, unknown,
cross-role or out-of-scope attempt is a critical agent-behaviour failure even though the
gateway denies it. The gateway never returns a secret record, a record that was not
available at the cutoff, or the values of a restricted record.
"""

from collections.abc import Mapping
from datetime import datetime

from pydantic import ValidationError

from tennis_engine.common.clock import Clock, require_aware

from .contracts import AccessClass, AgentRunContext, EvidenceRecord, sha256
from .model import ToolRequest, ToolResponse
from .proposals import AgentRecordStore, new_proposal
from .roles import RoleSpec
from .switch import AgentSwitch
from .tools import (
    CATALOG,
    FORBIDDEN_ACTIONS,
    ProposalArguments,
    ToolBackend,
    ToolKind,
    ToolSpec,
    ToolUnavailable,
    subject_ids,
)
from .trace import Status, TraceRecorder


def tool_label(name: str, catalog: Mapping[str, ToolSpec] = CATALOG) -> str:
    """A bounded label: a known or forbidden name, otherwise `unknown`."""
    return name if name in catalog or name in FORBIDDEN_ACTIONS else "unknown"


class ToolGateway:
    def __init__(
        self,
        *,
        context: AgentRunContext,
        spec: RoleSpec,
        backend: ToolBackend,
        store: AgentRecordStore,
        switch: AgentSwitch,
        clock: Clock,
        recorder: TraceRecorder,
        catalog: Mapping[str, ToolSpec] = CATALOG,
    ) -> None:
        self.context = context
        self.spec = spec
        self.backend = backend
        self.store = store
        self.switch = switch
        self.clock = clock
        self.recorder = recorder
        self.catalog = catalog
        self.seen: dict[str, EvidenceRecord] = {
            item.evidence_id: item for item in context.visible_evidence()
        }
        self.confirmed: list[str] = []
        self._cache: dict[str, ToolResponse] = {}
        # Set when the run must stop: BUDGET_EXHAUSTED, TIMEOUT or DISABLED.
        self.stop: Status | None = None

    # ------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return require_aware(self.clock.now())

    def _deny(
        self, request: ToolRequest, reason: str, *, critical: bool, outcome: str = "DENIED"
    ) -> ToolResponse:
        label = tool_label(request.tool, self.catalog)
        if critical:
            self.recorder.critical.append(f"{reason}:{label}")
        self.recorder.add(
            self._now(),
            "TOOL_CALL",
            outcome,
            tool=label,
            reason=reason,
            arguments_sha256=sha256(request.arguments),
            critical=int(critical),
        )
        return ToolResponse(
            call_id=request.call_id,
            tool=request.tool,
            outcome="DENIED" if outcome == "DENIED" else "BUDGET",
            reason=reason,
        )

    def _switch_reason(self, now: datetime) -> str | None:
        try:
            return self.switch.disabled(self.context.role, now)
        except Exception:  # noqa: BLE001 - a switch that cannot answer fails closed
            return "SWITCH_UNAVAILABLE"

    def _filter(self, records: tuple[EvidenceRecord, ...]) -> tuple[list[EvidenceRecord], int, int]:
        kept, future, secret = [], 0, 0
        for record in records:
            if record.access_class == AccessClass.SECRET:
                secret += 1
            elif record.available_at > self.context.as_of:
                future += 1
            else:
                kept.append(record.for_agent())
        return kept, future, secret

    # ------------------------------------------------------------------------------

    def call(self, request: ToolRequest) -> ToolResponse:
        now = self._now()
        budget = self.context.budget
        if self.recorder.tool_attempts >= budget.max_tool_calls:
            self.stop = "BUDGET_EXHAUSTED"
            return self._deny(request, "TOOL_BUDGET", critical=False, outcome="BUDGET")
        self.recorder.tool_attempts += 1
        if now >= self.context.deadline():
            self.stop = "TIMEOUT"
            return self._deny(request, "DEADLINE", critical=False, outcome="BUDGET")
        stopped = self._switch_reason(now)
        if stopped is not None:
            self.stop = "DISABLED"
            return self._deny(request, stopped, critical=False)
        if request.tool in FORBIDDEN_ACTIONS:
            return self._deny(request, "FORBIDDEN_ACTION", critical=True)
        spec = self.catalog.get(request.tool)
        if spec is None:
            return self._deny(request, "UNKNOWN_TOOL", critical=True)
        if request.tool not in self.spec.allowed_tools:
            return self._deny(request, "TOOL_NOT_ALLOWED", critical=True)
        try:
            arguments = spec.arguments.model_validate(request.arguments)
        except ValidationError:
            return self._error(request, spec, "INVALID_ARGUMENTS")
        subjects = subject_ids(arguments)
        if any(item not in self.context.authorized_scope.subject_ids for item in subjects):
            return self._deny(request, "OUT_OF_SCOPE", critical=True)
        if isinstance(arguments, ProposalArguments):
            if arguments.kind not in spec.proposal_kinds:
                return self._error(request, spec, "INVALID_ARGUMENTS")
            if any(item not in self.seen for item in arguments.evidence_ids):
                return self._deny(request, "UNSEEN_EVIDENCE", critical=True)
        key = sha256({"tool": request.tool, "arguments": arguments.model_dump(mode="json")})
        cached = self._cache.get(key)
        if cached is not None:
            self.recorder.add(
                now, "TOOL_CALL", "DEDUPLICATED", tool=spec.name, arguments_sha256=key
            )
            return cached.model_copy(update={"call_id": request.call_id})
        if spec.kind == ToolKind.PROPOSAL:
            assert isinstance(arguments, ProposalArguments)
            response = self._propose(request, spec, arguments, key)
        else:
            response = self._read(request, spec, subjects[0], key)
        if response.outcome in ("OK", "NOT_FOUND"):
            self._cache[key] = response
        return response

    def _error(self, request: ToolRequest, spec: ToolSpec, reason: str) -> ToolResponse:
        self.recorder.add(
            self._now(),
            "TOOL_CALL",
            "ERROR",
            tool=spec.name,
            reason=reason,
            arguments_sha256=sha256(request.arguments),
        )
        return ToolResponse(call_id=request.call_id, tool=spec.name, outcome="ERROR", reason=reason)

    def _read(self, request: ToolRequest, spec: ToolSpec, subject: str, key: str) -> ToolResponse:
        retries = 0
        while True:
            try:
                output = self.backend.read(spec.name, subject, self.context)
                break
            except ToolUnavailable:
                budget = self.context.budget
                if retries >= budget.max_retries or self.recorder.tool_attempts >= (
                    budget.max_tool_calls
                ):
                    return self._error(request, spec, "TOOL_UNAVAILABLE")
                retries += 1
                self.recorder.tool_attempts += 1
                self.recorder.add(
                    self._now(), "TOOL_CALL", "RETRY", tool=spec.name, arguments_sha256=key
                )
        kept, future, secret = self._filter(output.evidence)
        for record in kept:
            self.seen[record.evidence_id] = record
        self.recorder.add(
            self._now(),
            "TOOL_CALL",
            output.status,
            tool=spec.name,
            version=spec.version,
            arguments_sha256=key,
            subject=subject,
            evidence_ids=tuple(item.evidence_id for item in kept),
            withheld_future=future,
            withheld_secret=secret,
        )
        return ToolResponse(
            call_id=request.call_id, tool=spec.name, outcome=output.status, evidence=tuple(kept)
        )

    def _propose(
        self, request: ToolRequest, spec: ToolSpec, arguments: ProposalArguments, key: str
    ) -> ToolResponse:
        proposal = new_proposal(
            role=self.context.role,
            tool=spec.name,
            subject_id=arguments.subject_id,
            kind=arguments.kind,
            fields=dict(arguments.fields),
            evidence_ids=arguments.evidence_ids,
            rationale=arguments.rationale,
            trace_id=self.context.trace_id,
            created_at=self._now(),
        )
        try:
            stored, created = self.store.propose(proposal)
        except Exception:  # noqa: BLE001 - no confirmation without a stored proposal
            return self._error(request, spec, "STORE_UNAVAILABLE")
        proposal_id = str(stored.proposal_id)
        if proposal_id not in self.confirmed:
            self.confirmed.append(proposal_id)
        self.recorder.proposals = list(self.confirmed)
        self.recorder.add(
            self._now(),
            "TOOL_CALL",
            "OK",
            tool=spec.name,
            version=spec.version,
            arguments_sha256=key,
            subject=arguments.subject_id,
            proposal=proposal_id,
            created=int(created),
        )
        return ToolResponse(
            call_id=request.call_id, tool=spec.name, outcome="OK", proposal_id=proposal_id
        )
