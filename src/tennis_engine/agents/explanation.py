"""F14.6 explanation agent: rephrase or summarise the deterministic F14.5 explanation.

The agent sees only the statements that the viewer may see, so redistribution rules and
withheld values stay in force. A strict verifier accepts a sentence only when:

- it cites existing statements;
- each number is a number of a cited statement;
- each word is a word of the statements or an approved connective word;
- it keeps or omits negation exactly as the cited statements do;
- a number keeps the player that the statement binds it to;
- it names no decision other than the recorded one;
- it makes no comparison, certainty, motivation, injury or action claim of its own.

Any failure, timeout, budget exit or kill switch gives the deterministic text. The
closing decision statements stay deterministic; the agent cannot replace them.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from tennis_engine.common.clock import Clock, require_aware
from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.serving.contracts import (
    AgentProvenance,
    NarrativeSentence,
    Statement,
    StoredDecision,
)

from .contracts import AgentOutput, AgentRole, EvidenceRecord
from .model import LanguageModel
from .proposals import AgentRecordStore
from .roles import ROLES
from .runner import new_context, run_agent
from .switch import AgentSwitch
from .tools import StaticBackend
from .verify import NUMBER, Finding, Severity

WORD = re.compile(r"[A-Za-z_]+")
DECISION_LABELS = frozenset(item.value for item in RecommendationStatus)
NEGATIONS = frozenset({"not", "no", "never", "cannot", "without", "none"})
COMPARATIVES = frozenset(
    {
        "more", "less", "higher", "lower", "better", "worse", "fewer", "greater", "larger",
        "smaller", "stronger", "weaker", "above", "below", "over", "under", "leads", "ahead",
        "behind", "exceeds", "beats", "favourite", "favorite", "underdog",
    }
)  # fmt: skip
# Connective words for rephrasing. They add no fact, cause, source or certainty.
APPROVED_WORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at", "by", "with",
        "is", "are", "was", "has", "have", "this", "that", "these", "it", "its", "each",
        "both", "also", "here", "summary", "record", "records", "shows", "show", "gives",
        "states", "value", "values", "versus", "vs", "while", "which", "as", "from", "per",
        "then", "so", "overall", "recorded", "estimate", "estimates", "result", "results",
    }
)  # fmt: skip

STATEMENT_PREFIX = "statement-"


def statement_id(index: int) -> str:
    return f"{STATEMENT_PREFIX}{index}"


def statement_index(evidence_id: str) -> int | None:
    if not evidence_id.startswith(STATEMENT_PREFIX):
        return None
    tail = evidence_id[len(STATEMENT_PREFIX) :]
    return int(tail) if tail.isdigit() else None


def decision_values(stored: StoredDecision) -> dict[str, str]:
    record = stored.record
    stake = record.stake.amount if record.status == RecommendationStatus.BET else Decimal("0.00")
    values = {"decision": record.status.value, "recommended_stake": str(stake)}
    codes = sorted({gate.code.value for gate in record.gates if not gate.passed and gate.code})
    if codes:
        values["reason_codes"] = ",".join(codes)
    return values


def evidence_for(
    stored: StoredDecision, statements: Sequence[Statement]
) -> tuple[EvidenceRecord, ...]:
    at = stored.record.decided_at
    records = [
        EvidenceRecord(
            evidence_id=statement_id(index),
            kind="statement",
            available_at=at,
            values={"statement_kind": item.kind.lower()},
            text=item.text,
        )
        for index, item in enumerate(statements)
    ]
    records.append(
        EvidenceRecord(
            evidence_id="decision", kind="decision", available_at=at, values=decision_values(stored)
        )
    )
    return tuple(records)


def _numbers(text: str) -> list[str]:
    return [token.lstrip("+") for token in NUMBER.findall(text)]


def _bindings(text: str, names: Sequence[str]) -> dict[str, str]:
    """Numbers written as `<name>: <number>` in a canonical statement."""
    found: dict[str, str] = {}
    for name in names:
        for match in re.finditer(re.escape(name) + r": ([+-]?\d+(?:\.\d+)?)", text):
            found.setdefault(match.group(1).lstrip("+"), name)
    return found


def _nearest_name(text: str, position: int, names: Sequence[str]) -> str | None:
    best: tuple[int, str] | None = None
    for name in names:
        index = text.rfind(name, 0, position)
        if index >= 0 and (best is None or index > best[0]):
            best = (index, name)
    return best[1] if best else None


def verify_narrative(
    output: AgentOutput,
    statements: Sequence[Statement],
    decision: RecommendationStatus,
    names: Sequence[str],
) -> list[Finding]:
    texts = [item.text for item in statements]
    vocabulary = {word.lower() for text in texts for word in WORD.findall(text)} | APPROVED_WORDS
    findings: list[Finding] = []

    def check(text: str, cited: list[str], *, claim: bool) -> None:
        cited_text = " ".join(cited)
        words = WORD.findall(text)
        lowered = {word.lower() for word in words}
        if lowered - vocabulary:
            findings.append(Finding("UNSUPPORTED_WORD", Severity.MAJOR))
        allowed_numbers = set(_numbers(cited_text))
        for match in NUMBER.finditer(text):
            number = match.group(0).lstrip("+")
            if number not in allowed_numbers:
                findings.append(Finding("UNSUPPORTED_NUMBER", Severity.CRITICAL))
                continue
            bound = _bindings(cited_text, names).get(number)
            near = _nearest_name(text, match.start(), names)
            if bound is not None and near is not None and near != bound:
                findings.append(Finding("SWAPPED_ORIENTATION", Severity.CRITICAL))
        cited_negation = any(
            word.lower() in NEGATIONS for item in cited for word in WORD.findall(item)
        )
        added = bool(lowered & NEGATIONS) and not cited_negation
        dropped = claim and cited_negation and not lowered & NEGATIONS
        if added or dropped:
            findings.append(Finding("NEGATION_CHANGED", Severity.CRITICAL))
        if lowered & COMPARATIVES and text.strip() not in {item.strip() for item in cited}:
            findings.append(Finding("UNSUPPORTED_COMPARISON", Severity.MAJOR))
        labels = {word for word in words if word in DECISION_LABELS}
        if labels - {decision.value}:
            findings.append(Finding("DECISION_MISMATCH", Severity.CRITICAL))

    all_cited: list[str] = []
    for claim in output.claims:
        cited = []
        for evidence_id in claim.evidence_ids:
            index = statement_index(evidence_id)
            if evidence_id == "decision":
                continue
            if index is None or index >= len(statements):
                findings.append(Finding("FABRICATED_EVIDENCE", Severity.CRITICAL))
                continue
            cited.append(texts[index])
        if not cited:
            findings.append(Finding("NO_STATEMENT_CITED", Severity.MAJOR))
        all_cited += cited
        check(claim.text, cited, claim=True)
    check(output.summary, all_cited or texts, claim=False)
    if output.status == "COMPLETED" and output.decision != decision:
        findings.append(Finding("DECISION_MISMATCH", Severity.CRITICAL))
    return findings


@dataclass(frozen=True)
class NarrativeResult:
    source: str
    narrative: tuple[NarrativeSentence, ...] | None
    fallback_reason: str | None
    agent: AgentProvenance | None


@dataclass
class ExplanationNarrator:
    """Runs the AG-EX role for one stored decision. It never raises to the caller."""

    model: LanguageModel
    switch: AgentSwitch
    store: AgentRecordStore
    clock: Clock
    max_tokens: int
    max_cost: Decimal
    metrics: object | None = None

    def narrate(self, stored: StoredDecision, statements: Sequence[Statement]) -> NarrativeResult:
        spec = ROLES[AgentRole.EXPLANATION]
        try:
            now = require_aware(self.clock.now())
            evidence = evidence_for(stored, statements)
            subject = str(stored.record.decision_id)
            context = new_context(
                spec,
                task="Rephrase the deterministic explanation in plain language. Cite the "
                "statements. Keep every number and the decision exactly.",
                subject_ids=(subject,),
                as_of=now,
                started_at=now,
                model=self.model.ref,
                budget=spec.budget(max_tokens=self.max_tokens, max_cost=self.max_cost),
                evidence=evidence,
            )
            names = [player.display_name for player in stored.context.match.players]

            def extra(output: AgentOutput, seen: Mapping[str, EvidenceRecord]) -> list[Finding]:
                return verify_narrative(output, statements, stored.record.status, names)

            result = run_agent(
                context,
                spec=spec,
                model=self.model,
                backend=StaticBackend({("get_explanation", subject): evidence}),
                store=self.store,
                switch=self.switch,
                clock=self.clock,
                extra_verifier=extra,
            )
        except Exception as error:  # noqa: BLE001 - the deterministic text always serves
            return NarrativeResult(
                "DETERMINISTIC", None, f"AGENT_ERROR:{type(error).__name__}", None
            )
        record = getattr(self.metrics, "record", None)
        if callable(record):
            try:
                record(result.trace)
            except Exception:  # noqa: BLE001 - metrics never change the served text
                pass
        provenance = AgentProvenance(
            role_version=spec.version,
            prompt_sha256=spec.prompt_sha256,
            model_id=f"{self.model.ref.provider}/{self.model.ref.model_id}",
            trace_id=result.trace.trace_id,
            status=result.status,
        )
        output = result.output
        if not result.usable or output is None or output.status != "COMPLETED":
            reason = result.status if result.status != "COMPLETED" else "TRACE_UNAVAILABLE"
            if output is not None and output.status != "COMPLETED":
                reason = output.status
            return NarrativeResult("DETERMINISTIC", None, reason, provenance)
        sentences = tuple(
            NarrativeSentence(
                text=claim.text,
                statement_indexes=tuple(
                    index
                    for index in (statement_index(item) for item in claim.evidence_ids)
                    if index is not None
                ),
            )
            for claim in output.claims
        )
        return NarrativeResult("AGENT", sentences, None, provenance)
