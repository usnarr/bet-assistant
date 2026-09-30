"""F18.7 evaluation runner, aggregation, human-review queue and release decision.

Stages: load and check the manifest, build frozen fake tools, run the bounded agent,
collect the trace, score it deterministically, queue human review, aggregate by role,
family and group, and emit a release decision.

A release decision is `PASS` only when every gate passes for every role, no trajectory has
a critical violation, the gates are frozen, a sealed release set ran the required number
of times and every required human review is complete. A harness error, a missing budget
cap, a missing role or a pending review gives `BLOCKED`. A critical violation, a failed
gate or a human disagreement gives `FAIL`.
"""

import hashlib
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from tennis_engine.agents.contracts import AgentRole
from tennis_engine.agents.proposals import InMemoryAgentStore
from tennis_engine.agents.roles import ROLES
from tennis_engine.agents.runner import new_context, run_agent
from tennis_engine.agents.switch import StaticSwitch
from tennis_engine.agents.trace import AgentTrace
from tennis_engine.common.clock import FrozenClock
from tennis_engine.common.contracts import Contract, ExactDecimal, Identifier
from tennis_engine.common.ids import stable_id

from .agents import build_agent
from .cases import EvalCase, FixtureBackend
from .scoring import CaseScore, score

Purpose = Literal["smoke", "integration", "release"]
PREFIXES = tuple(role.prefix for role in AgentRole) + ("AG-X",)


class Gates(Contract):
    critical_violations_max: Annotated[int, Field(ge=0)] = 0
    structured_validity_min: ExactDecimal
    numeric_fidelity_min: ExactDecimal
    evidence_fidelity_min: ExactDecimal
    abstention_recall_min: ExactDecimal
    benign_completion_min: ExactDecimal
    unnecessary_refusal_max: ExactDecimal
    tool_appropriateness_min: ExactDecimal
    budget_compliance_min: ExactDecimal


class ModelCaps(Contract):
    max_tokens: Annotated[int, Field(ge=1)] | None = None
    max_cost: Annotated[ExactDecimal, Field(ge=0)] | None = None


class EvalConfig(Contract):
    schema_version: Literal["1.0"] = "1.0"
    version: Identifier
    status: Literal["PROPOSED", "FROZEN"]
    reason: str
    gates: Gates
    release_repetitions: Annotated[int, Field(ge=1)]
    smoke_min_cases: Annotated[int, Field(ge=1)]
    human_review_fraction: Annotated[ExactDecimal, Field(gt=0, le=1)]
    # Key: provider/model_id. A model without both caps blocks the benchmark.
    models: dict[str, ModelCaps]

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class Rate(Contract):
    numerator: int
    denominator: int
    rate: Decimal | None
    lower95: Decimal | None
    upper95: Decimal | None


def _quantize(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)


def rate(numerator: int, denominator: int) -> Rate:
    """Wilson 95% interval. Small denominators give wide intervals."""
    if denominator == 0:
        return Rate(numerator=0, denominator=0, rate=None, lower95=None, upper95=None)
    z = 1.959964
    p = numerator / denominator
    centre = (p + z * z / (2 * denominator)) / (1 + z * z / denominator)
    half = (
        z
        * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator * denominator))
        / (1 + z * z / denominator)
    )
    return Rate(
        numerator=numerator,
        denominator=denominator,
        rate=_quantize(p),
        lower95=_quantize(max(0.0, centre - half)),
        upper95=_quantize(min(1.0, centre + half)),
    )


class GroupMetrics(Contract):
    cases: int
    trajectories: int
    critical_trajectories: int
    critical_cases_worst_repetition: int
    protected_trajectories: int
    structured_validity: Rate
    numeric_fidelity: Rate
    evidence_fidelity: Rate
    abstention_recall: Rate
    benign_completion: Rate
    benign_completion_worst_repetition: Rate
    unnecessary_refusal: Rate
    tool_appropriateness: Rate
    budget_compliance: Rate
    pass_rate: Rate
    tool_attempts: int
    tokens: int
    estimated_cost: Decimal
    latency_p50_seconds: Decimal | None
    latency_p95_seconds: Decimal | None


def _count(scores: Sequence[CaseScore], field: str, positive: bool = True) -> Rate:
    values = [getattr(item, field) for item in scores if getattr(item, field) is not None]
    return rate(sum(1 for item in values if item is positive), len(values))


def _percentile(values: list[Decimal], fraction: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(Decimal(len(ordered)) * fraction) - 1)
    return ordered[index]


def metrics(scores: Sequence[CaseScore]) -> GroupMetrics:
    by_case: dict[str, list[CaseScore]] = defaultdict(list)
    for item in scores:
        by_case[item.case_id].append(item)
    benign_cases = [items for items in by_case.values() if items[0].benign_completed is not None]
    latencies = [item.latency_seconds for item in scores]
    return GroupMetrics(
        cases=len(by_case),
        trajectories=len(scores),
        critical_trajectories=sum(1 for item in scores if item.critical_violations),
        critical_cases_worst_repetition=sum(
            1 for items in by_case.values() if any(item.critical_violations for item in items)
        ),
        protected_trajectories=sum(1 for item in scores if item.protected),
        structured_validity=_count(scores, "structured_valid"),
        numeric_fidelity=_count(scores, "numeric_fidelity"),
        evidence_fidelity=_count(scores, "evidence_fidelity"),
        abstention_recall=_count(scores, "abstention_correct"),
        benign_completion=_count(scores, "benign_completed"),
        benign_completion_worst_repetition=rate(
            sum(1 for items in benign_cases if all(item.benign_completed for item in items)),
            len(benign_cases),
        ),
        unnecessary_refusal=_count(scores, "unnecessary_refusal"),
        tool_appropriateness=_count(scores, "tool_appropriate"),
        budget_compliance=_count(scores, "budget_compliant"),
        pass_rate=_count(scores, "passed"),
        tool_attempts=sum(item.tool_attempts for item in scores),
        tokens=sum(item.tokens for item in scores),
        estimated_cost=sum((item.estimated_cost for item in scores), Decimal(0)),
        latency_p50_seconds=_percentile(latencies, Decimal("0.5")),
        latency_p95_seconds=_percentile(latencies, Decimal("0.95")),
    )


def gate_failures(name: str, group: GroupMetrics, gates: Gates) -> list[str]:
    failures = []
    if group.critical_trajectories > gates.critical_violations_max:
        failures.append(f"{name}:CRITICAL_VIOLATIONS")
    minimums = {
        "structured_validity": gates.structured_validity_min,
        "numeric_fidelity": gates.numeric_fidelity_min,
        "evidence_fidelity": gates.evidence_fidelity_min,
        "abstention_recall": gates.abstention_recall_min,
        "benign_completion": gates.benign_completion_min,
        "tool_appropriateness": gates.tool_appropriateness_min,
        "budget_compliance": gates.budget_compliance_min,
    }
    for field, minimum in minimums.items():
        measured: Rate = getattr(group, field)
        if measured.rate is not None and measured.rate < minimum:
            failures.append(f"{name}:{field.upper()}")
    refusal = group.unnecessary_refusal
    if refusal.rate is not None and refusal.rate > gates.unnecessary_refusal_max:
        failures.append(f"{name}:UNNECESSARY_REFUSAL")
    return failures


class Adjudication(Contract):
    case_id: str
    repetition: int
    trace_id: str
    reason: Literal["CRITICAL", "NEAR_MISS", "SAMPLE"]
    reviewer: str | None = None
    verdict: Literal["PENDING", "AGREE", "DISAGREE"] = "PENDING"
    notes: str | None = None


def review_queue(scores: Sequence[CaseScore], fraction: Decimal) -> list[Adjudication]:
    """Every critical failure and near miss, plus a stratified sample of the rest."""
    queue: list[Adjudication] = []
    rest: dict[tuple[str, str], list[CaseScore]] = defaultdict(list)
    for item in scores:
        if item.critical_violations:
            reason: Literal["CRITICAL", "NEAR_MISS", "SAMPLE"] | None = "CRITICAL"
        elif item.near_miss:
            reason = "NEAR_MISS"
        else:
            reason = None
            rest[(item.prefix, item.group)].append(item)
        if reason is not None:
            queue.append(
                Adjudication(
                    case_id=item.case_id,
                    repetition=item.repetition,
                    trace_id=item.trace_id,
                    reason=reason,
                )
            )
    for _, items in sorted(rest.items()):
        wanted = max(1, math.ceil(Decimal(len(items)) * fraction))
        chosen = sorted(
            items,
            key=lambda item: hashlib.sha256(f"{item.case_id}:{item.repetition}".encode()).digest(),
        )[:wanted]
        queue += [
            Adjudication(
                case_id=item.case_id,
                repetition=item.repetition,
                trace_id=item.trace_id,
                reason="SAMPLE",
            )
            for item in chosen
        ]
    return queue


def merge_reviews(
    queue: Iterable[Adjudication], reviews: Iterable[Adjudication]
) -> list[Adjudication]:
    done = {(item.case_id, item.repetition, item.trace_id): item for item in reviews}
    merged = []
    for item in queue:
        found = done.get((item.case_id, item.repetition, item.trace_id))
        merged.append(found if found is not None and found.reviewer else item)
    return merged


class ReleaseDecision(Contract):
    decision: Literal["PASS", "FAIL", "BLOCKED"]
    purpose: Purpose
    failures: tuple[str, ...]
    blocked: tuple[str, ...]
    config_version: str
    config_status: str
    agent: str
    repetitions: int
    trajectories: int
    signed_by: None = None


class SuiteResult(Contract):
    scores: tuple[CaseScore, ...]
    traces: tuple[AgentTrace, ...]
    errors: tuple[str, ...]
    overall: GroupMetrics
    by_role: dict[str, GroupMetrics]
    by_family: dict[str, GroupMetrics]
    by_group: dict[str, GroupMetrics]
    adjudications: tuple[Adjudication, ...]
    decision: ReleaseDecision


class BlockedError(RuntimeError):
    """A missing prerequisite. The case cannot run, so the suite is BLOCKED."""


def run_case(
    case: EvalCase, agent_name: str, repetition: int, config: EvalConfig
) -> tuple[CaseScore, AgentTrace]:
    clock = FrozenClock(case.as_of)
    agent = build_agent(agent_name, case.role, clock, case.model_delay_seconds)
    caps = config.models.get(f"{agent.ref.provider}/{agent.ref.model_id}")
    if caps is None or caps.max_tokens is None or caps.max_cost is None:
        raise BlockedError("BUDGET_CAPS_MISSING")
    spec = ROLES[case.role]
    context = new_context(
        spec,
        task=case.task,
        subject_ids=case.subject_ids,
        as_of=case.as_of,
        started_at=case.as_of,
        model=agent.ref,
        budget=spec.budget(max_tokens=caps.max_tokens, max_cost=caps.max_cost),
        evidence=case.evidence_bundle,
        expires_at=case.expires_at,
        run_id=stable_id("agent-eval-run", f"{case.case_id}:{case.version}:{repetition}"),
    )
    store = InMemoryAgentStore()
    switch = StaticSwitch({case.role: "AGENT_STOPPED"} if case.agent_stopped else None)
    result = run_agent(
        context,
        spec=spec,
        model=agent,
        backend=FixtureBackend(case.tool_fixtures),
        store=store,
        switch=switch,
        clock=clock,
    )
    return score(case, result, store, repetition), result.trace


def run_suite(
    cases: Sequence[EvalCase],
    *,
    agent: str,
    config: EvalConfig,
    purpose: Purpose,
    repetitions: int,
    reviews: Iterable[Adjudication] = (),
) -> SuiteResult:
    scores: list[CaseScore] = []
    traces: list[AgentTrace] = []
    errors: list[str] = []
    for repetition in range(1, repetitions + 1):
        for case in cases:
            try:
                item, trace = run_case(case, agent, repetition, config)
            except BlockedError as error:
                errors.append(f"{case.case_id}:{error}")
                continue
            except Exception as error:  # noqa: BLE001 - a crash blocks, never passes
                errors.append(f"{case.case_id}:HARNESS_ERROR:{type(error).__name__}")
                continue
            scores.append(item)
            traces.append(trace)
    by_role = {
        prefix: metrics([item for item in scores if item.prefix == prefix]) for prefix in PREFIXES
    }
    families = sorted({item.family_id for item in scores})
    by_family = {
        family: metrics([item for item in scores if item.family_id == family])
        for family in families
    }
    groups = ("valid", "incomplete", "adversarial", "cross")
    by_group = {
        group: metrics([item for item in scores if item.group == group]) for group in groups
    }
    overall = metrics(scores)
    adjudications = merge_reviews(review_queue(scores, config.human_review_fraction), reviews)

    failures: list[str] = []
    blocked: list[str] = sorted({error.split(":", 1)[1] for error in errors})
    for prefix, group in by_role.items():
        if group.trajectories == 0:
            blocked.append(f"ROLE_NOT_COVERED:{prefix}")
        failures += gate_failures(prefix, group, config.gates)
    failures += gate_failures("ALL", overall, config.gates)
    failures += [
        f"HUMAN_DISAGREES:{item.case_id}" for item in adjudications if item.verdict == "DISAGREE"
    ]
    if any(item.verdict == "PENDING" for item in adjudications):
        blocked.append("HUMAN_REVIEW_PENDING")
    if config.status != "FROZEN":
        blocked.append("GATES_NOT_FROZEN")
    if purpose == "release":
        if not any(item.split == "release" for item in cases):
            blocked.append("SEALED_RELEASE_SET_MISSING")
        if repetitions < config.release_repetitions:
            blocked.append("REPETITIONS_BELOW_RELEASE")
    elif purpose == "smoke" and len(cases) < config.smoke_min_cases:
        blocked.append("SMOKE_SUBSET_TOO_SMALL")
    if not scores:
        blocked.append("NO_TRAJECTORIES")
    decision: Literal["PASS", "FAIL", "BLOCKED"] = (
        "FAIL" if failures else "BLOCKED" if blocked else "PASS"
    )
    return SuiteResult(
        scores=tuple(scores),
        traces=tuple(traces),
        errors=tuple(errors),
        overall=overall,
        by_role=by_role,
        by_family=by_family,
        by_group=by_group,
        adjudications=tuple(adjudications),
        decision=ReleaseDecision(
            decision=decision,
            purpose=purpose,
            failures=tuple(sorted(set(failures))),
            blocked=tuple(sorted(set(blocked))),
            config_version=config.version,
            config_status=config.status,
            agent=agent,
            repetitions=repetitions,
            trajectories=len(scores),
        ),
    )


def load_config(path: Path) -> EvalConfig:
    return EvalConfig.model_validate_json(path.read_bytes())
