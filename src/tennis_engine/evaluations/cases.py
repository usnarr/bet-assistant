"""F18.5 evaluation cases, frozen fake tools and the fixture manifest.

A case holds the task, the cutoff, the evidence bundle, the deterministic tool responses
and the independent expected outcome. The expected outcome is written by the fixture
author from the deterministic rules. It never comes from an agent answer.

The manifest lists each case file with its SHA-256 and split. A changed file, a missing
file, a duplicated case ID or a family in two splits blocks the run. Sealed release cases
are not in this repository: they come from a separate location at run time.
"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from tennis_engine.agents.contracts import AgentRole, EvidenceRecord
from tennis_engine.agents.tools import ToolOutput, ToolUnavailable
from tennis_engine.agents.trace import Status
from tennis_engine.common.contracts import Contract, Identifier, Timestamp
from tennis_engine.contracts.domain import RecommendationStatus

Split = Literal["development", "validation", "release"]
CrossFamily = Literal["injection", "temporal", "authorization", "retries", "resource", "handoff"]


class ToolFixture(Contract):
    tool: Identifier
    subject_id: Identifier
    records: tuple[EvidenceRecord, ...] = ()
    status: Literal["OK", "NOT_FOUND"] = "OK"
    # Injected transient failures before the first success.
    fail_first: Annotated[int, Field(ge=0, le=10)] = 0


class Expectation(Contract):
    """Independent ground truth for one case."""

    statuses: tuple[Status, ...] = Field(min_length=1)
    # A valid supported task: a refusal is an unnecessary refusal.
    benign: bool
    # A mandatory prerequisite fails: the agent must abstain, escalate or stop.
    hard_failure: bool = False
    decision: RecommendationStatus | None = None
    recommended_stake: str | None = None
    reason_codes: tuple[str, ...] = ()
    required_evidence: tuple[Identifier, ...] = ()
    # Evidence the agent must not cite, for example later or unapproved records.
    forbidden_evidence: tuple[Identifier, ...] = ()
    required_tools: tuple[Identifier, ...] = ()
    expected_facts: dict[Identifier, str] = Field(default_factory=dict)
    proposal_kind: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.benign and self.hard_failure:
            raise ValueError("A case cannot be benign and a hard failure")
        return self


class EvalCase(Contract):
    schema_version: Literal["1.0"] = "1.0"
    case_id: Annotated[str, Field(pattern=r"^AG-(DI|ID|RF|MA|VR|EX|MO|X)-[A-Z0-9-]+$")]
    family_id: Identifier
    version: Annotated[int, Field(ge=1)]
    split: Split
    role: AgentRole
    group: Literal["valid", "incomplete", "adversarial", "cross"]
    cross_family: CrossFamily | None = None
    tags: tuple[Identifier, ...] = ()
    severity: Literal["critical", "major", "minor"]
    task: Annotated[str, Field(min_length=1, max_length=2000)]
    as_of: Timestamp
    expires_at: Timestamp | None = None
    subject_ids: tuple[Identifier, ...] = Field(min_length=1)
    evidence_bundle: tuple[EvidenceRecord, ...] = ()
    tool_fixtures: tuple[ToolFixture, ...] = ()
    expected: Expectation
    forbidden_actions: tuple[Identifier, ...] = ()
    required_constraints: tuple[str, ...] = ()
    # Harness settings: simulated model latency and a kill-switch state.
    model_delay_seconds: Annotated[int, Field(ge=0, le=600)] = 0
    agent_stopped: bool = False
    oracle_version: Identifier
    reviewer: str | None = None
    provenance: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def cross_family_named(self) -> Self:
        if (self.group == "cross") != (self.cross_family is not None):
            raise ValueError("A cross-role case names exactly one cross family")
        prefix = "AG-X" if self.group == "cross" else self.role.prefix
        if not self.case_id.startswith(prefix + "-"):
            raise ValueError("The case ID prefix does not match the role or group")
        return self

    @property
    def prefix(self) -> str:
        return "AG-X" if self.group == "cross" else self.role.prefix


class FixtureBackend:
    """Frozen fake tools. Each (tool, subject) has fixed records and injected failures."""

    def __init__(self, fixtures: tuple[ToolFixture, ...]) -> None:
        self.fixtures: Mapping[tuple[str, str], ToolFixture] = {
            (item.tool, item.subject_id): item for item in fixtures
        }
        self.failures: dict[tuple[str, str], int] = {}

    def read(self, tool: str, subject_id: str, context: object) -> ToolOutput:
        fixture = self.fixtures.get((tool, subject_id))
        if fixture is None:
            return ToolOutput(status="NOT_FOUND")
        key = (tool, subject_id)
        if self.failures.get(key, 0) < fixture.fail_first:
            self.failures[key] = self.failures.get(key, 0) + 1
            raise ToolUnavailable()
        return ToolOutput(status=fixture.status, evidence=fixture.records)


class ManifestEntry(Contract):
    path: Annotated[str, Field(pattern=r"^[a-z]+/[A-Za-z0-9_.-]+\.json$")]
    case_id: str
    split: Split
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Manifest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    fixture_set: Identifier
    provenance: str
    cases: tuple[ManifestEntry, ...]


class FixtureError(ValueError):
    """The fixture set is not valid. The run is BLOCKED."""


def file_sha256(path: Path) -> str:
    """SHA-256 of the file with LF line ends, so a CRLF checkout gives the same hash."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_cases(root: Path, splits: tuple[Split, ...]) -> tuple[Manifest, tuple[EvalCase, ...]]:
    manifest = Manifest.model_validate_json((root / "manifest.json").read_bytes())
    cases: list[EvalCase] = []
    for entry in manifest.cases:
        if entry.split not in splits:
            continue
        path = root / entry.path
        if not path.is_file():
            raise FixtureError(f"Missing fixture {entry.path}")
        if file_sha256(path) != entry.sha256:
            raise FixtureError(f"Fixture hash mismatch {entry.path}")
        case = EvalCase.model_validate_json(path.read_bytes())
        if case.case_id != entry.case_id or case.split != entry.split:
            raise FixtureError(f"Manifest entry does not match {entry.path}")
        cases.append(case)
    check_cases(tuple(cases))
    return manifest, tuple(cases)


def check_cases(cases: tuple[EvalCase, ...]) -> None:
    ids = [item.case_id for item in cases]
    if len(set(ids)) != len(ids):
        raise FixtureError("Duplicate case IDs")
    families: dict[str, str] = {}
    for item in cases:
        previous = families.setdefault(item.family_id, item.split)
        if previous != item.split:
            raise FixtureError(f"Family {item.family_id} is in two splits")


def split_hashes(cases: tuple[EvalCase, ...]) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for item in cases:
        grouped.setdefault(item.split, []).append(
            hashlib.sha256(item.model_dump_json().encode()).hexdigest()
        )
    return {
        split: hashlib.sha256(json.dumps(sorted(values)).encode()).hexdigest()
        for split, values in sorted(grouped.items())
    }
