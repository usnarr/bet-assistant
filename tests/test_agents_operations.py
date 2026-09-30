"""F15.6 agent kill switch on the F01 journal, audited traces and agent metrics."""

import pytest
from agent_support import SECRET_VALUE, call, final, record, run, turn

from tennis_engine.agents.contracts import AccessClass, AgentRole
from tennis_engine.agents.governance import GovernanceAgentSwitch, agent_stop_key
from tennis_engine.agents.switch import RoleFlags
from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.monitoring.instruments import AgentMetrics
from tennis_engine.monitoring.metrics import MetricsRegistry


def test_kill_switch_uses_the_existing_governance_stops(store, clock, tmp_path):
    switch = GovernanceAgentSwitch(lambda: store)
    # An empty journal has the global stop on: every agent is off.
    assert switch.disabled(AgentRole.EXPLANATION, clock()) == "GLOBAL_DISABLE"
    store.set_global_disable(False, reason="SYS fixture: start shadow operation")
    assert switch.disabled(AgentRole.EXPLANATION, clock()) is None

    operator = GovernanceStore(
        tmp_path / "governance.sqlite3", Principal(identity="fixture-operator", role=Role.OPERATOR)
    )
    operator.clock = clock
    operator.set_source_stop(agent_stop_key(AgentRole.VALUE_RISK), True, reason="AG-VR incident")
    assert agent_stop_key(AgentRole.VALUE_RISK) == "agent:ag-vr"
    assert switch.disabled(AgentRole.VALUE_RISK, clock()) == "AGENT_STOPPED"
    assert switch.disabled(AgentRole.EXPLANATION, clock()) is None
    # An operator stops; only a reviewer resumes.
    with pytest.raises(PermissionError):
        operator.set_source_stop(agent_stop_key(AgentRole.VALUE_RISK), False, reason="resume")
    operator.close()
    store.set_source_stop(agent_stop_key(AgentRole.VALUE_RISK), False, reason="Reviewed resume")
    assert switch.disabled(AgentRole.VALUE_RISK, clock()) is None

    store.set_global_disable(True, reason="Global stop")
    assert switch.disabled(AgentRole.EXPLANATION, clock()) == "GLOBAL_DISABLE"
    result, model = run([turn(final=final())], switch=GovernanceAgentSwitch(lambda: store))
    assert result.status == "DISABLED" and model.requests == []


def test_kill_switch_fails_closed(clock):
    def broken():
        raise OSError("journal unavailable")

    switch = GovernanceAgentSwitch(broken)
    assert switch.disabled(AgentRole.RESEARCH, clock()) == "SWITCH_UNAVAILABLE"
    flags = RoleFlags({AgentRole.RESEARCH}, switch)
    assert flags.disabled(AgentRole.RESEARCH, clock()) == "SWITCH_UNAVAILABLE"
    assert flags.disabled(AgentRole.MONITORING, clock()) == "ROLE_NOT_ENABLED"


def test_agent_metrics_count_denied_attempts_without_sensitive_data():
    secret = record(
        "synthetic-key", "credential", token=SECRET_VALUE, access_class=AccessClass.SECRET
    )
    result, _ = run(
        [turn(call("evaluate_quote"), call("place_bet"), call("x" * 60)), turn(final=final())],
        evidence=(secret,),
    )
    registry = MetricsRegistry()
    AgentMetrics(registry).record(result.trace)
    body = registry.render()
    assert 'tennis_agent_runs_total{role="value_risk_reviewer",status="REJECTED"} 1' in body
    assert (
        'tennis_agent_critical_attempts_total{role="value_risk_reviewer",'
        'reason="FORBIDDEN_ACTION"} 1'
    ) in body
    assert 'tool="unknown",outcome="DENIED"' in body
    assert 'tennis_agent_fallbacks_total{role="value_risk_reviewer"} 1' in body
    assert SECRET_VALUE not in body and "x" * 60 not in body
    assert SECRET_VALUE not in result.trace.model_dump_json()
