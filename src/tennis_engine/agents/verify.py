"""F18.4 deterministic output verification.

The verifier trusts only records that the run actually received. It checks that each
citation exists and was available at the cutoff, that each quoted number equals a cited
record, that a deterministic decision and stake are reproduced exactly, that no action is
claimed without a tool confirmation, and that no certainty, motivation, injury or secret
appears without support. A critical or major finding rejects the output.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from tennis_engine.contracts.domain import RecommendationStatus

from .contracts import AccessClass, AgentOutput, AgentRunContext, EvidenceRecord, as_decimal
from .roles import RoleSpec


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    detail: str = ""


BLOCKING = frozenset({Severity.CRITICAL, Severity.MAJOR})

# A number that is not part of an identifier such as `quote-1` or `f18`.
NUMBER = re.compile(r"(?<![A-Za-z0-9_\-.])[+-]?\d+(?:\.\d+)?(?![A-Za-z0-9_]|\.\d)")

CERTAINTY = re.compile(
    r"\b(guarantee[sd]?|certain to|certainly|sure (win|thing|bet)|a lock|risk[- ]free|"
    r"can(no|')t lose|cannot lose|definitely|destined|no[- ]brainer|easy money|sure to)\b",
    re.IGNORECASE,
)
MOTIVATION = re.compile(
    r"\b(wants? it more|wants? to win|motivat\w*|hungr(y|ier)|desperate|revenge|"
    r"mental(ly)? (strong|weak|tough))\b",
    re.IGNORECASE,
)
INJURY = re.compile(r"\b(injur\w*|hurt|pain|illness|ill|sick|fitness|medical)\b", re.IGNORECASE)
FALSE_COMPLETION = re.compile(
    r"\b(i|we) (have )?(placed|submitted|merged|approved|re-?enabled|resumed|closed|promoted|"
    r"deployed|changed)\b|\bbets? (was|were|has been|have been|is) (placed|submitted)\b",
    re.IGNORECASE,
)


def numbers(text: str) -> list[Decimal]:
    found = []
    for token in NUMBER.findall(text):
        value = as_decimal(token)
        if value is not None:
            found.append(value)
    return found


def record_numbers(records: Iterable[EvidenceRecord]) -> set[Decimal]:
    found: set[Decimal] = set()
    for record in records:
        for value in record.values.values():
            number = as_decimal(value)
            if number is not None:
                found.add(number)
            found.update(numbers(value))
        if record.text:
            found.update(numbers(record.text))
    return found


def same_value(left: str, right: str) -> bool:
    a, b = as_decimal(left), as_decimal(right)
    if a is not None and b is not None:
        return a == b
    return left.strip() == right.strip()


def _citation_problem(
    evidence_id: str, context: AgentRunContext, seen: Mapping[str, EvidenceRecord]
) -> Finding | None:
    if evidence_id in seen:
        return None
    bundle = {item.evidence_id: item for item in context.evidence}
    record = bundle.get(evidence_id)
    if record is not None and record.available_at > context.as_of:
        return Finding("FUTURE_EVIDENCE", Severity.CRITICAL, evidence_id)
    if record is not None and record.access_class == AccessClass.SECRET:
        return Finding("SECRET_EVIDENCE", Severity.CRITICAL, evidence_id)
    return Finding("FABRICATED_EVIDENCE", Severity.CRITICAL, evidence_id)


def _language(text: str, cited: list[EvidenceRecord]) -> list[Finding]:
    findings = []
    if CERTAINTY.search(text):
        findings.append(Finding("UNSUPPORTED_CERTAINTY", Severity.CRITICAL))
    if MOTIVATION.search(text):
        findings.append(Finding("UNSUPPORTED_MOTIVATION", Severity.CRITICAL))
    if INJURY.search(text) and not any(item.kind == "injury_report" for item in cited):
        findings.append(Finding("UNSUPPORTED_INJURY", Severity.CRITICAL))
    if FALSE_COMPLETION.search(text):
        findings.append(Finding("FALSE_COMPLETION", Severity.CRITICAL))
    return findings


def _secrets(output: AgentOutput, context: AgentRunContext) -> list[Finding]:
    body = output.model_dump_json()
    findings = []
    for record in context.evidence:
        if record.access_class not in (AccessClass.SECRET, AccessClass.RESTRICTED):
            continue
        minimum = 8 if record.access_class == AccessClass.SECRET else 6
        pieces = [*record.values.values(), *([record.text] if record.text else [])]
        if any(len(piece) >= minimum and piece in body for piece in pieces):
            code = "SECRET_LEAK" if record.access_class == AccessClass.SECRET else "RESTRICTED_LEAK"
            findings.append(Finding(code, Severity.CRITICAL, record.evidence_id))
    return findings


def _decision(
    output: AgentOutput, spec: RoleSpec, seen: Mapping[str, EvidenceRecord]
) -> list[Finding]:
    decisions = [item for item in seen.values() if item.kind == "decision" and not item.withheld]
    findings: list[Finding] = []
    if not decisions:
        if output.decision is not None or output.recommended_stake not in (None, Decimal(0)):
            findings.append(Finding("UNSUPPORTED_DECISION", Severity.CRITICAL))
        return findings
    labels = {item.values.get("decision") for item in decisions}
    if len(labels) != 1 or None in labels:
        # Conflicting deterministic records: only abstention or review is valid.
        if output.status == "COMPLETED":
            findings.append(Finding("CONFLICT_NOT_ESCALATED", Severity.CRITICAL))
        return findings
    canonical = RecommendationStatus(str(next(iter(labels))))
    record = decisions[-1]
    stake = as_decimal(record.values.get("recommended_stake", "0")) or Decimal(0)
    if output.decision == RecommendationStatus.BET and canonical != RecommendationStatus.BET:
        findings.append(Finding("HARD_GATE_OVERRIDE", Severity.CRITICAL))
    elif output.decision is not None and output.decision != canonical:
        findings.append(Finding("DECISION_MISMATCH", Severity.CRITICAL))
    if spec.reproduces_decision and output.status == "COMPLETED" and output.decision is None:
        findings.append(Finding("DECISION_MISSING", Severity.MAJOR))
    if output.recommended_stake is not None:
        if output.recommended_stake != stake:
            findings.append(Finding("STAKE_MISMATCH", Severity.CRITICAL))
        if output.recommended_stake > 0 and canonical != RecommendationStatus.BET:
            findings.append(Finding("STAKE_WITHOUT_BET", Severity.CRITICAL))
    allowed = {
        code for item in decisions for code in item.values.get("reason_codes", "").split(",")
    }
    extra = [code for code in output.reason_codes if code not in allowed]
    if extra:
        findings.append(Finding("UNSUPPORTED_REASON", Severity.MAJOR))
    return findings


def verify_output(
    output: AgentOutput,
    *,
    context: AgentRunContext,
    spec: RoleSpec,
    seen: Mapping[str, EvidenceRecord],
    confirmed_proposals: Iterable[str],
) -> list[Finding]:
    findings: list[Finding] = []
    all_cited: list[EvidenceRecord] = []
    for claim in output.claims:
        cited = []
        for evidence_id in claim.evidence_ids:
            problem = _citation_problem(evidence_id, context, seen)
            if problem is not None:
                findings.append(problem)
            else:
                cited.append(seen[evidence_id])
        all_cited.extend(cited)
        for key, value in claim.values.items():
            candidates = [item.values[key] for item in cited if key in item.values]
            if not candidates:
                findings.append(Finding("UNSUPPORTED_VALUE", Severity.CRITICAL, key))
            elif not any(same_value(value, item) for item in candidates):
                findings.append(Finding("NUMBER_MISMATCH", Severity.CRITICAL, key))
        supported = record_numbers(cited)
        if any(number not in supported for number in numbers(claim.text)):
            findings.append(Finding("UNSUPPORTED_NUMBER", Severity.CRITICAL))
        findings.extend(_language(claim.text, cited))
    supported = record_numbers(all_cited)
    if any(number not in supported for number in numbers(output.summary)):
        findings.append(Finding("UNSUPPORTED_NUMBER", Severity.CRITICAL, "summary"))
    findings.extend(_language(output.summary, all_cited))
    confirmed = set(confirmed_proposals)
    if any(item not in confirmed for item in output.proposal_ids):
        findings.append(Finding("UNCONFIRMED_ACTION", Severity.CRITICAL))
    findings.extend(_decision(output, spec, seen))
    findings.extend(_secrets(output, context))
    if output.status == "COMPLETED" and not output.claims:
        findings.append(Finding("NO_EVIDENCE", Severity.MAJOR))
    return findings
