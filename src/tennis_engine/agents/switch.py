"""Agent enable flags (F18.8) and the agent kill switch.

`disabled(role, now)` returns a reason code, or None when the role may run. A switch that
cannot answer must return a reason: it fails closed. The runner checks the switch before
the first model call, and the gateway checks it before every tool call.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from .contracts import AgentRole


class AgentSwitch(Protocol):
    def disabled(self, role: AgentRole, now: datetime) -> str | None: ...


class StaticSwitch:
    """Fixed state, for tests. `stopped` maps a role to a reason code."""

    def __init__(self, stopped: dict[AgentRole, str] | None = None) -> None:
        self.stopped = dict(stopped or {})

    def disabled(self, role: AgentRole, now: datetime) -> str | None:
        return self.stopped.get(role)


class RoleFlags:
    """F18.8: only roles that passed their evaluation are enabled. Default: none."""

    def __init__(self, enabled: Iterable[AgentRole], inner: AgentSwitch | None = None) -> None:
        self.enabled = frozenset(enabled)
        self.inner = inner

    def disabled(self, role: AgentRole, now: datetime) -> str | None:
        if role not in self.enabled:
            return "ROLE_NOT_ENABLED"
        if self.inner is None:
            return None
        try:
            return self.inner.disabled(role, now)
        except Exception:  # noqa: BLE001 - a switch that cannot answer fails closed
            return "SWITCH_UNAVAILABLE"
