"""F15 operator commands: alert evaluation and deterministic controls.

Exit codes: 0 no alert, 1 at least one alert, 2 an error. See docs/operations/README.md.
"""

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from tennis_engine.governance.cli import resolve_principal
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.monitoring.alerts import evaluate, load_rules
from tennis_engine.monitoring.controls import ControlAction, apply_controls
from tennis_engine.monitoring.signals import Signal

DEFAULT_RULES = Path("configs/operations/alert-rules.json")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="tennis-ops", description="F15 operations")
    commands = root.add_subparsers(dest="command", required=True)
    alerts = commands.add_parser("evaluate-alerts", help="Evaluate signals against alert rules")
    alerts.add_argument("--signals", type=Path, required=True, help="JSON list of signals")
    alerts.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    alerts.add_argument("--sources", default="", help="Comma-separated expected source IDs")
    alerts.add_argument("--apply", action="store_true", help="Apply critical controls")
    alerts.add_argument("--database", type=Path, default=Path("var/governance.sqlite3"))
    alerts.add_argument("--access-file", type=Path, default=Path("var/governance-access.json"))
    return root


def evaluate_alerts(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    signals = TypeAdapter(tuple[Signal, ...]).validate_json(args.signals.read_bytes())
    sources = [item for item in args.sources.split(",") if item]
    found = evaluate(rules, signals, sources=sources, now=datetime.now(UTC))
    actions: tuple[ControlAction, ...] = ()
    if args.apply and found:
        store = GovernanceStore(args.database, resolve_principal(args.access_file))
        try:
            actions = apply_controls(found, store)
        finally:
            store.close()
    print(
        json.dumps(
            {
                "rule_set": rules.version,
                "rule_set_status": rules.status,
                "alerts": [item.model_dump(mode="json") for item in found],
                "actions": [item.model_dump(mode="json") for item in actions],
            },
            indent=2,
        )
    )
    return 1 if found else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return evaluate_alerts(args)
    except ValidationError as error:
        # Do not echo input values; they can hold operational data.
        print(json.dumps({"error": "INVALID_INPUT", "fields": [e["loc"] for e in error.errors()]}))
        return 2
    except (OSError, ValueError, PermissionError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
