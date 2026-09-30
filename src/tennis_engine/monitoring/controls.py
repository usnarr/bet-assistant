"""F15.4 deterministic controls: a critical alert stops the affected recommendation path.

Controls use the existing F01 stops only:

- `SOURCE_STOP` appends a source stop. `can_fetch` then denies the source with
  `SOURCE_STOPPED`, and F14 read checks serve every record from it as `NO_BET`.
- `GLOBAL_STOP` turns the global stop on. Responsible-use lookups then deny every scope.

The caller supplies a governance store with an operator principal. A control never
resumes anything; only a reviewer can do that through the governance CLI. Applying the
same alerts again appends nothing (the stop is already on).
"""

from collections.abc import Iterable
from typing import Literal

from tennis_engine.common.contracts import Contract, Identifier
from tennis_engine.governance.store import GovernanceStore

from .alerts import Alert, Control, Severity


class ControlAction(Contract):
    rule_id: Identifier
    control: Control
    scope: Identifier
    outcome: Literal["APPLIED", "ALREADY_STOPPED"]
    revision: int | None


def apply_controls(alerts: Iterable[Alert], store: GovernanceStore) -> tuple[ControlAction, ...]:
    actions: list[ControlAction] = []
    for alert in alerts:
        if alert.severity != Severity.CRITICAL or alert.control == Control.NONE:
            continue
        now = store.clock()
        reason = (
            f"F15 alert {alert.rule_id} ({alert.reason}) on {alert.signal} for {alert.scope}; "
            f"rule set {alert.rule_set_version}"
        )
        if alert.control == Control.GLOBAL_STOP:
            if store.global_disabled(now):
                actions.append(_action(alert, "ALREADY_STOPPED", None))
            else:
                revision = store.set_global_disable(True, reason=reason)
                actions.append(_action(alert, "APPLIED", revision))
        else:
            if store.source_stopped(alert.scope, now):
                actions.append(_action(alert, "ALREADY_STOPPED", None))
            else:
                revision = store.set_source_stop(alert.scope, True, reason=reason)
                actions.append(_action(alert, "APPLIED", revision))
    return tuple(actions)


def _action(
    alert: Alert, outcome: Literal["APPLIED", "ALREADY_STOPPED"], revision: int | None
) -> ControlAction:
    return ControlAction(
        rule_id=alert.rule_id,
        control=alert.control,
        scope=alert.scope,
        outcome=outcome,
        revision=revision,
    )
