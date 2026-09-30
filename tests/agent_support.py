"""Synthetic F18 fixtures: evidence, contexts and scripted fake models.

All names, values and secrets are synthetic. No model provider is called.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tennis_engine.agents.contracts import AccessClass, AgentRole, EvidenceRecord
from tennis_engine.agents.model import FAKE_MODEL, ModelTurn, ScriptedModel, ToolRequest
from tennis_engine.agents.proposals import InMemoryAgentStore
from tennis_engine.agents.roles import ROLES
from tennis_engine.agents.runner import new_context, run_agent
from tennis_engine.agents.switch import StaticSwitch
from tennis_engine.agents.tools import StaticBackend
from tennis_engine.common.clock import FrozenClock

AS_OF = datetime(2026, 8, 2, 10, tzinfo=UTC)
SUBJECT = "synthetic-decision-1"
SECRET_VALUE = "synthetic-secret-canary-7f3a"


def record(evidence_id, kind="fact", *, at=AS_OF - timedelta(minutes=5), **values):
    access = values.pop("access_class", AccessClass.INTERNAL)
    text = values.pop("text", None)
    return EvidenceRecord(
        evidence_id=evidence_id,
        kind=kind,
        source_id="synthetic-source",
        available_at=at,
        access_class=access,
        values={key: str(value) for key, value in values.items()},
        text=text,
    )


def decision_record(decision="NO_BET", stake="0.00", reasons="STALE_QUOTE", **extra):
    return record(
        "synthetic-evaluation-1",
        "decision",
        decision=decision,
        recommended_stake=stake,
        reason_codes=reasons,
        **extra,
    )


def call(tool, subject=SUBJECT, call_id="c1", **arguments):
    return ToolRequest(call_id=call_id, tool=tool, arguments={"subject_id": subject} | arguments)


def turn(*calls, final=None):
    return ModelTurn(tool_calls=calls, final=final, input_tokens=100, output_tokens=50)


def final(status="COMPLETED", decision="NO_BET", stake="0.00", claims=None, **extra):
    body = {
        "status": status,
        "summary": "The deterministic evaluator returned the decision.",
        "claims": claims
        if claims is not None
        else [
            {
                "text": "The evaluator decision is recorded.",
                "evidence_ids": ["synthetic-evaluation-1"],
                "values": {"decision": decision},
            }
        ],
        "decision": decision,
        "recommended_stake": stake,
        "reason_codes": ["STALE_QUOTE"],
    }
    if status != "COMPLETED":
        body["abstention_reason"] = "Synthetic reason."
    return body | extra


def context(
    role=AgentRole.VALUE_RISK,
    *,
    evidence=(),
    expires_at=None,
    max_tokens=10_000,
    max_cost="0",
    model=FAKE_MODEL,
    subjects=(SUBJECT,),
    **budget,
):
    spec = ROLES[role]
    return new_context(
        spec,
        task="Review the synthetic decision with the supplied tools.",
        subject_ids=subjects,
        as_of=AS_OF,
        started_at=AS_OF,
        model=model,
        budget=spec.budget(max_tokens=max_tokens, max_cost=Decimal(max_cost), **budget),
        evidence=evidence,
        expires_at=expires_at,
    )


def run(
    steps,
    *,
    role=AgentRole.VALUE_RISK,
    records=None,
    store=None,
    switch=None,
    clock=None,
    delay=0.0,
    ctx=None,
    **kwargs,
):
    clock = clock or FrozenClock(AS_OF)
    backend = StaticBackend(
        records if records is not None else {("evaluate_quote", SUBJECT): (decision_record(),)}
    )
    model = ScriptedModel(steps, clock=clock, delay_seconds=delay)
    result = run_agent(
        ctx or context(role, **kwargs),
        spec=ROLES[role],
        model=model,
        backend=backend,
        store=store or InMemoryAgentStore(),
        switch=switch or StaticSwitch(),
        clock=clock,
    )
    return result, model
