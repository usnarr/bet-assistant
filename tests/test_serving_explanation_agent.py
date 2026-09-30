"""F14.6 explanation agent: verified rephrasing, strict fallback and serving rules."""

from decimal import Decimal

import pytest
from serving_support import BOOK_SOURCE, FakeChecks, bet, build, headers, no_bet, stored

from tennis_engine.agents.contracts import AgentRole
from tennis_engine.agents.explanation import ExplanationNarrator
from tennis_engine.agents.model import ModelTimeout, ModelTurn, ScriptedModel
from tennis_engine.agents.proposals import InMemoryAgentStore
from tennis_engine.agents.switch import RoleFlags, StaticSwitch
from tennis_engine.governance.contracts import Role
from tennis_engine.monitoring.metrics import MetricsRegistry
from tennis_engine.serving.api import Serving, create_app
from tennis_engine.serving.explain import explain

ELO = (
    "Model estimate. Surface Elo rating. Player Alpha: 1850.5 Elo points; "
    "Player Beta: 1778.0 Elo points. Difference: +72.5 Elo points."
)


def path(item):
    return f"/v1/tennis/recommendations/{item.record.decision_id}/explanation"


def final(claims, decision="BET", stake="29.75", summary="Here is a summary of the records."):
    return ModelTurn(
        final={
            "status": "COMPLETED",
            "summary": summary,
            "claims": [
                {"text": text, "evidence_ids": [f"statement-{index}"]} for text, index in claims
            ],
            "decision": decision,
            "recommended_stake": stake,
        },
        input_tokens=300,
        output_tokens=120,
    )


def setup(
    steps, item=None, *, switch=None, delay=0.0, redistribution=(BOOK_SOURCE, "synthetic-sports")
):
    item = item or stored(bet())
    client, service, _, clock = build([item], redistribution=redistribution)
    model = ScriptedModel(steps, clock=clock, delay_seconds=delay)
    store = InMemoryAgentStore()
    service.narrator = ExplanationNarrator(
        model=model,
        switch=switch or StaticSwitch(),
        store=store,
        clock=clock,
        max_tokens=5000,
        max_cost=Decimal("0"),
    )
    return client, service, model, store, item


def get(client, item, role=Role.DASHBOARD):
    response = client.get(path(item), headers=headers(role))
    assert response.status_code == 200, response.text
    return response.json()


def test_without_agent_the_deterministic_text_serves():
    item = stored(bet())
    client, service, _, _ = build([item])
    body = get(client, item)
    assert body["source"] == "DETERMINISTIC"
    assert body["fallback_reason"] == "AGENT_NOT_CONFIGURED"
    assert body["narrative"] is None
    expected = explain(item, lambda source: False)
    assert [s["text"] for s in body["statements"]] == [s.text for s in expected]


def test_verified_rephrasing_is_served_with_provenance():
    claims = [
        (
            "The model estimate of the Surface Elo rating: Player Alpha: 1850.5 Elo points; "
            "Player Beta: 1778.0 Elo points. Difference: +72.5 Elo points.",
            1,
        ),
        (
            "Matches played in the last 90 days. Player Alpha: 14 matches; Player Beta: 9 matches.",
            2,
        ),
        ("Minutes played in the last 48 hours: no verified value.", 3),
    ]
    client, _, model, store, item = setup([final(claims)])
    body = get(client, item)
    assert body["source"] == "AGENT", body["fallback_reason"]
    assert [s["statement_indexes"] for s in body["narrative"]] == [[1], [2], [3]]
    assert body["agent"]["status"] == "COMPLETED"
    assert body["agent"]["model_id"] == "fake/scripted-fake"
    # The deterministic statements, including the decision, are always present.
    assert body["statements"][-1]["text"].startswith("Decision: BET.")
    assert body["decision"] == "BET" and body["actionable"] is True
    assert len(store.traces) == 1
    # The agent received the canonical statements as data.
    request = model.requests[0]
    assert any(record.text == ELO for record in request.evidence)


@pytest.mark.parametrize(
    ("claims", "kwargs", "finding"),
    [
        ([("Player Alpha: 1851 Elo points.", 1)], {}, "UNSUPPORTED_NUMBER"),
        (
            [("Player Beta: 1850.5 Elo points; Player Alpha: 1778.0 Elo points.", 1)],
            {},
            "SWAPPED_ORIENTATION",
        ),
        ([("Player Alpha wants it more.", 1)], {}, "UNSUPPORTED_MOTIVATION"),
        ([("Player Beta has an injury.", 1)], {}, "UNSUPPORTED_INJURY"),
        ([("A win is guaranteed.", 7)], {}, "UNSUPPORTED_CERTAINTY"),
        ([("According to a news report, Player Alpha is rested.", 2)], {}, "UNSUPPORTED_WORD"),
        ([("Player Alpha has a higher Surface Elo rating.", 1)], {}, "UNSUPPORTED_COMPARISON"),
        ([("Minutes played in the last 48 hours: a verified value.", 3)], {}, "NEGATION_CHANGED"),
        ([("The decision is NO_BET.", 7)], {}, "DECISION_MISMATCH"),
        ([("The record shows the Surface Elo rating.", 42)], {}, "FABRICATED_EVIDENCE"),
        (
            [("The record shows the Surface Elo rating.", 1)],
            {"decision": "WATCH", "stake": "0.00"},
            "DECISION_MISMATCH",
        ),
        ([("The record shows the Surface Elo rating.", 1)], {"stake": "100.00"}, "STAKE_MISMATCH"),
    ],
)
def test_verifier_rejects_and_falls_back(claims, kwargs, finding):
    client, service, _, store, item = setup([final(claims, **kwargs)])
    body = get(client, item)
    assert body["source"] == "DETERMINISTIC"
    assert body["narrative"] is None
    assert body["fallback_reason"] == "REJECTED"
    (trace,) = store.traces.values()
    assert finding in trace.findings


def test_withheld_values_never_reach_the_agent():
    # The dashboard role may not see values of sources without redistribution rights.
    claims = [("synthetic-book quoted 2.30 for Player Alpha.", 0)]
    client, _, model, store, item = setup([final(claims)], redistribution=())
    body = get(client, item, Role.DASHBOARD)
    assert body["statements"][0]["kind"] == "WITHHELD"
    assert all("2.30" not in (record.text or "") for record in model.requests[0].evidence)
    assert body["source"] == "DETERMINISTIC"
    (trace,) = store.traces.values()
    assert "UNSUPPORTED_NUMBER" in trace.findings


def test_kill_switch_timeout_and_errors_fall_back():
    stopped = StaticSwitch({AgentRole.EXPLANATION: "AGENT_STOPPED"})
    client, _, model, _, item = setup([final([])], switch=stopped)
    body = get(client, item)
    assert body["source"] == "DETERMINISTIC" and body["fallback_reason"] == "DISABLED"
    assert model.requests == []

    client, _, _, _, item = setup([final([])], switch=RoleFlags(()))
    assert get(client, item)["fallback_reason"] == "DISABLED"

    client, _, _, _, item = setup([ModelTimeout(), ModelTimeout()])
    assert get(client, item)["fallback_reason"] == "TIMEOUT"

    claims = [("Minutes played in the last 48 hours: no verified value.", 3)]
    client, _, _, _, item = setup([final(claims)], delay=11.0)
    assert get(client, item)["fallback_reason"] == "TIMEOUT"

    class Broken:
        ref = ScriptedModel([]).ref

        def complete(self, request):
            raise RuntimeError("provider exploded")

    client, service, _, _, item = setup([])
    service.narrator.model = Broken()
    assert get(client, item)["fallback_reason"] == "MODEL_UNAVAILABLE"


def test_narrative_cannot_make_a_blocked_record_actionable():
    checks = FakeChecks()
    checks.disabled = {BOOK_SOURCE: "SOURCE_STOPPED"}
    item = stored(bet())
    client, service, _, clock = build([item], checks=checks)
    claims = [("Minutes played in the last 48 hours: no verified value.", 3)]
    service.narrator = ExplanationNarrator(
        model=ScriptedModel([final(claims)], clock=clock),
        switch=StaticSwitch(),
        store=InMemoryAgentStore(),
        clock=clock,
        max_tokens=5000,
        max_cost=Decimal("0"),
    )
    body = get(client, item)
    assert body["recorded_decision"] == "BET"
    assert body["decision"] == "NO_BET" and body["actionable"] is False
    assert body["read_time_reasons"]


def test_no_bet_narrative_keeps_negation_and_decision():
    item = stored(no_bet())
    claims = [
        ("The central expected value is not positive.", 7),
        ("Expected value: -0.1370. Conservative expected value: -0.3440.", 6),
    ]
    client, _, _, _, item = setup([final(claims, decision="NO_BET", stake="0.00")], item)
    body = get(client, item)
    assert body["source"] == "AGENT", body
    assert body["decision"] == "NO_BET"


def test_route_permissions_and_errors():
    client, _, _, _, item = setup([final([])])
    assert client.get(path(item)).status_code == 401
    missing = "/v1/tennis/recommendations/00000000-0000-0000-0000-000000000000/explanation"
    response = client.get(missing, headers=headers())
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post(path(item), headers=headers()).status_code == 405


def test_agent_metrics_are_registered_with_the_app():
    item = stored(bet())
    _, service, _, clock = build([item])
    service.narrator = ExplanationNarrator(
        model=ScriptedModel([ModelTimeout(), ModelTimeout()], clock=clock),
        switch=StaticSwitch(),
        store=InMemoryAgentStore(),
        clock=clock,
        max_tokens=5000,
        max_cost=Decimal("0"),
    )
    registry = MetricsRegistry()
    from fastapi.testclient import TestClient
    from serving_support import authenticator
    from test_foundation_api import Probe

    from tennis_engine.infrastructure.settings import Settings

    app = create_app(
        Settings(environment="test"), Probe(True), Serving(service, authenticator()), registry
    )
    client = TestClient(app)
    assert client.get(path(item), headers=headers()).status_code == 200
    text = registry.render()
    assert 'tennis_agent_runs_total{role="explanation_assistant",status="TIMEOUT"} 1' in text
    assert 'tennis_agent_fallbacks_total{role="explanation_assistant"} 1' in text
