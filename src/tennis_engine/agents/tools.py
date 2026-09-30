"""F18.1 and F18.3 versioned tool schemas.

The catalog has read tools and proposal tools only. It has no SQL, shell, network, bet
placement, policy, merge, approval or re-enable tool. A name outside the catalog is
denied. A name in `FORBIDDEN_ACTIONS` is a critical attempt.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Protocol

from pydantic import Field

from tennis_engine.common.contracts import Contract, Identifier

from .contracts import AgentRunContext, EvidenceRecord, Value

TOOL_SCHEMA_VERSION = "agent-tools-v1"

# Actions that no agent may take. They are not in the catalog, so the gateway denies
# them. An attempt is still an agent-behaviour failure.
FORBIDDEN_ACTIONS = frozenset(
    {
        "place_bet",
        "submit_bet",
        "change_risk_policy",
        "change_stake",
        "override_decision",
        "commit_identity_merge",
        "approve_source",
        "enable_source",
        "resume_source",
        "approve_model",
        "promote_model",
        "alter_split",
        "edit_feature_value",
        "close_incident",
        "deploy_fix",
        "change_credentials",
        "read_secret",
        "run_sql",
        "shell",
        "http_request",
        "spawn_agent",
    }
)


ProposalKind = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]


class ToolKind(StrEnum):
    READ = "READ"
    PROPOSAL = "PROPOSAL"


class SubjectArguments(Contract):
    subject_fields: ClassVar[tuple[str, ...]] = ("subject_id",)
    subject_id: Identifier


class ProposalArguments(Contract):
    """A review-queue proposal. It needs evidence; it changes no canonical record."""

    subject_fields: ClassVar[tuple[str, ...]] = ("subject_id",)
    subject_id: Identifier
    kind: ProposalKind
    evidence_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=20)
    fields: dict[Identifier, Value] = Field(default_factory=dict, max_length=20)
    rationale: Annotated[str, Field(min_length=1, max_length=500)]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    kind: ToolKind
    arguments: type[SubjectArguments] | type[ProposalArguments]
    description: str
    # Allowed proposal kinds. Empty for read tools.
    proposal_kinds: frozenset[str] = frozenset()


def _read(name: str, description: str) -> ToolSpec:
    return ToolSpec(name, "v1", ToolKind.READ, SubjectArguments, description)


def _proposal(name: str, description: str, kinds: set[str]) -> ToolSpec:
    return ToolSpec(name, "v1", ToolKind.PROPOSAL, ProposalArguments, description, frozenset(kinds))


CATALOG: Mapping[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        _read("get_payload_report", "Sanitized schema and volume report of one raw response."),
        _proposal(
            "propose_dead_letter",
            "Propose a dead-letter classification or a parser mapping for review.",
            {"QUARANTINE_SCHEMA_DRIFT", "QUARANTINE_VOLUME_ANOMALY", "ACCESS_STOP", "MAPPING"},
        ),
        _read("get_identity_candidates", "Permitted candidates and context of one review item."),
        _proposal(
            "propose_identity_review",
            "Suggest a mapping with evidence, or escalate. Never commits a merge.",
            {"SUGGEST_MAPPING", "ESCALATE_AMBIGUOUS"},
        ),
        _read("get_facts_at_cutoff", "Approved facts of one match available at the cutoff."),
        _read("get_evaluation_report", "One immutable evaluation report."),
        _proposal(
            "propose_model_card_draft",
            "Propose a model-card draft or flag an invalid claim. Never promotes.",
            {"MODEL_CARD_DRAFT", "INVALID_CLAIM"},
        ),
        _read("get_recommendation_audit", "The stored decision record of one recommendation."),
        _read("evaluate_quote", "The deterministic decision, values and gates of one decision."),
        _read("get_explanation", "The deterministic explanation statements of one decision."),
        _read("get_telemetry", "Signals and alerts of one monitoring scope."),
        _proposal(
            "propose_incident_triage",
            "Propose triage and a reviewed runbook step. Never closes or re-enables.",
            {"TRIAGE", "FALSE_ALARM", "ESCALATE"},
        ),
    )
}


class ToolOutput(Contract):
    status: Literal["OK", "NOT_FOUND"]
    evidence: tuple[EvidenceRecord, ...] = ()


class ToolUnavailable(Exception):
    """A transient backend failure or timeout. The gateway may retry within budget."""


class ToolBackend(Protocol):
    """Deterministic read services behind the read tools."""

    def read(self, tool: str, subject_id: str, context: AgentRunContext) -> ToolOutput: ...


class EmptyBackend:
    """A backend with no records. Every read returns NOT_FOUND."""

    def read(self, tool: str, subject_id: str, context: AgentRunContext) -> ToolOutput:
        return ToolOutput(status="NOT_FOUND")


class StaticBackend:
    """Fixed records by (tool, subject). For fixtures and simple deterministic services."""

    def __init__(self, records: Mapping[tuple[str, str], tuple[EvidenceRecord, ...]]) -> None:
        self.records = dict(records)

    def read(self, tool: str, subject_id: str, context: AgentRunContext) -> ToolOutput:
        found = self.records.get((tool, subject_id))
        if found is None:
            return ToolOutput(status="NOT_FOUND")
        return ToolOutput(status="OK", evidence=found)


def subject_ids(arguments: SubjectArguments | ProposalArguments) -> tuple[str, ...]:
    return tuple(str(getattr(arguments, name)) for name in arguments.subject_fields)
