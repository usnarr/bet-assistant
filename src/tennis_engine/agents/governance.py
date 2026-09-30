"""F15.6 agent kill switch on the existing F01 governance journal."""

from collections.abc import Callable
from datetime import datetime

from tennis_engine.governance.store import GovernanceStore

from .contracts import AgentRole


def agent_stop_key(role: AgentRole) -> str:
    """The F01 stop key of one role, for example `agent:ag-ex`."""
    return f"agent:{role.prefix.lower()}"


class GovernanceAgentSwitch:
    """F15.6 kill switch on the existing F01 journal. It adds no new stop mechanism.

    - The global stop disables every agent role.
    - `set_source_stop(agent_stop_key(role), True)` disables one role. An operator can
      stop a role (`tennis-governance source-stop agent:ag-ex on`); only a policy reviewer
      can resume it, as for a source.
    - A journal that cannot be read disables the role.
    """

    def __init__(self, store: Callable[[], GovernanceStore]) -> None:
        self.store = store

    def disabled(self, role: AgentRole, now: datetime) -> str | None:
        try:
            journal = self.store()
            if journal.global_disabled(now):
                return "GLOBAL_DISABLE"
            if journal.source_stopped(agent_stop_key(role), now):
                return "AGENT_STOPPED"
        except Exception:  # noqa: BLE001 - a journal that cannot answer fails closed
            return "SWITCH_UNAVAILABLE"
        return None
