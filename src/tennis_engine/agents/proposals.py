"""F18.3 idempotent review-queue proposals and trace storage.

A proposal is the only write an agent can cause. It has state `PROPOSED` and no path to
apply it: a human reviewer acts on it through the owning deterministic service. The
idempotency key covers the role, tool, subject, kind, fields and evidence, so a retry or a
duplicate request returns the first proposal and writes nothing new.
"""

import threading
from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field

from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp
from tennis_engine.common.ids import stable_id

from .contracts import AgentRole, Value, sha256
from .tools import ProposalKind
from .trace import AgentTrace


class Proposal(Contract):
    proposal_id: UUID
    idempotency_key: Digest
    role: AgentRole
    tool: Identifier
    subject_id: Identifier
    kind: ProposalKind
    fields: dict[Identifier, Value] = Field(default_factory=dict)
    evidence_ids: tuple[Identifier, ...]
    rationale: Annotated[str, Field(min_length=1, max_length=500)]
    trace_id: UUID
    created_at: Timestamp
    state: Literal["PROPOSED"] = "PROPOSED"


def proposal_key(
    role: AgentRole,
    tool: str,
    subject_id: str,
    kind: str,
    fields: dict[str, str],
    evidence_ids: tuple[str, ...],
) -> str:
    return sha256(
        {
            "role": role.value,
            "tool": tool,
            "subject_id": subject_id,
            "kind": kind,
            "fields": fields,
            "evidence_ids": sorted(set(evidence_ids)),
        }
    )


def new_proposal(
    *,
    role: AgentRole,
    tool: str,
    subject_id: str,
    kind: str,
    fields: dict[str, str],
    evidence_ids: tuple[str, ...],
    rationale: str,
    trace_id: UUID,
    created_at: datetime,
) -> Proposal:
    key = proposal_key(role, tool, subject_id, kind, fields, evidence_ids)
    return Proposal(
        proposal_id=stable_id("agent-proposal", key),
        idempotency_key=key,
        role=role,
        tool=tool,
        subject_id=subject_id,
        kind=kind,
        fields=fields,
        evidence_ids=tuple(sorted(set(evidence_ids))),
        rationale=rationale,
        trace_id=trace_id,
        created_at=created_at,
    )


class AgentRecordStore(Protocol):
    def propose(self, proposal: Proposal) -> tuple[Proposal, bool]:
        """Store a proposal once. Return the stored proposal and True when it is new."""
        ...

    def record_trace(self, trace: AgentTrace) -> bool:
        """Store a trace once. Return True when it is new."""
        ...


class TraceConflict(ValueError):
    """Other content under an existing trace ID. Traces are immutable."""


class InMemoryAgentStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proposals: dict[str, Proposal] = {}
        self.traces: dict[UUID, AgentTrace] = {}

    def propose(self, proposal: Proposal) -> tuple[Proposal, bool]:
        with self._lock:
            existing = self.proposals.get(proposal.idempotency_key)
            if existing is not None:
                return existing, False
            self.proposals[proposal.idempotency_key] = proposal
            return proposal, True

    def record_trace(self, trace: AgentTrace) -> bool:
        with self._lock:
            existing = self.traces.get(trace.trace_id)
            if existing is not None:
                if existing != trace:
                    raise TraceConflict("A trace is immutable")
                return False
            self.traces[trace.trace_id] = trace
            return True
