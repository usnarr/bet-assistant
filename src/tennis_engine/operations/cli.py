"""F15 operator commands: alerts, controls, incidents, recovery checks and roles.

Exit codes: 0 no alert, 1 at least one alert, 2 an error. See docs/operations/README.md.
"""

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from tennis_engine.common.logging import configure_logging
from tennis_engine.governance.cli import resolve_principal
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.database import build_engine
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.monitoring.alerts import evaluate, load_rules
from tennis_engine.monitoring.controls import ControlAction, apply_controls
from tennis_engine.monitoring.signals import Signal
from tennis_engine.serving.postgres import PostgresDecisionStore

from .incidents import Category, open_incident, verify_bundle
from .recovery import (
    DatabaseFingerprint,
    JournalFingerprint,
    Objectives,
    backup_journal,
    compare,
    database_fingerprint,
    journal_fingerprint,
)
from .roles import grant_serving_reader

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

    fingerprint = commands.add_parser(
        "fingerprint", help="Row counts and digests of every table (TENNIS_DATABASE_URL)"
    )
    fingerprint.add_argument("--output", type=Path, required=True)
    journal = commands.add_parser("backup-journal", help="Consistent copy of the F01 journal")
    journal.add_argument("--source", type=Path, required=True)
    journal.add_argument("--target", type=Path, required=True)
    verify = commands.add_parser(
        "verify-restore", help="Compare a restored database with a source fingerprint"
    )
    verify.add_argument("--expected", type=Path, required=True)
    verify.add_argument("--journal-source", type=Path)
    verify.add_argument("--journal-restored", type=Path)
    verify.add_argument("--rto-seconds", type=int)
    verify.add_argument("--rpo-seconds", type=int)
    verify.add_argument("--measured-restore-seconds", type=float)
    verify.add_argument("--measured-data-loss-seconds", type=float)

    incident = commands.add_parser("open-incident", help="Stop, then preserve evidence")
    incident.add_argument("--category", required=True, choices=[c.value for c in Category])
    incident.add_argument("--severity", default="CRITICAL", choices=["CRITICAL", "WARNING"])
    incident.add_argument("--summary", required=True)
    incident.add_argument("--window-start", type=datetime.fromisoformat, required=True)
    incident.add_argument("--window-end", type=datetime.fromisoformat, required=True)
    incident.add_argument("--source", action="append", default=[])
    incident.add_argument("--bookmaker", action="append", default=[])
    incident.add_argument("--stop-sources", action="store_true")
    incident.add_argument("--global-stop", action="store_true")
    incident.add_argument("--database", type=Path, default=Path("var/governance.sqlite3"))
    incident.add_argument("--access-file", type=Path, default=Path("var/governance-access.json"))
    check = commands.add_parser("verify-incident", help="Check an incident bundle's hashes")
    check.add_argument("directory", type=Path)

    reader = commands.add_parser(
        "grant-reader", help="Grant SELECT-only access for the API to an existing role"
    )
    reader.add_argument("--role", required=True)

    scheduler = commands.add_parser(
        "scheduler", help="Run the F15 job graph and operations tasks on a cadence"
    )
    scheduler.add_argument("--once", action="store_true", help="Run one tick and exit")
    scheduler.add_argument("--tick-seconds", type=float, default=60.0)
    scheduler.add_argument("--metrics-port", type=int, default=9101)
    scheduler.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    scheduler.add_argument("--signal-inbox", type=Path, default=Path("var/signals"))
    scheduler.add_argument(
        "--no-apply", action="store_true", help="Evaluate alerts but apply no control"
    )
    return root


def scheduler_command(args: argparse.Namespace) -> int:
    from .runtime import SchedulerOptions, build_scheduler, run_scheduler

    settings = Settings()
    configure_logging(getattr(logging, settings.log_level))
    options = SchedulerOptions(
        tick_seconds=args.tick_seconds,
        # One tick serves no scrape, so it takes any free port.
        metrics_port=0 if args.once else args.metrics_port,
        rules=args.rules,
        signal_inbox=args.signal_inbox,
        apply_controls=not args.no_apply,
    )
    process = build_scheduler(settings, options)
    if args.once:
        try:
            report = process.scheduler.tick()
        finally:
            process.close()
        _print(report.model_dump(mode="json"))
        return 0
    run_scheduler(process)
    return 0


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


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def recovery_command(args: argparse.Namespace) -> int:
    settings = Settings()
    now = datetime.now(UTC)
    if args.command == "backup-journal":
        backup_journal(args.source, args.target)
        _print(journal_fingerprint(args.target).model_dump(mode="json"))
        return 0
    engine = build_engine(settings.database_url)
    try:
        if args.command == "fingerprint":
            taken = database_fingerprint(engine, now)
            args.output.write_text(taken.model_dump_json(indent=2) + "\n", encoding="utf-8")
            _print({"revision": taken.revision, "tables": len(taken.tables)})
            return 0
        if args.command == "grant-reader":
            grant_serving_reader(engine, args.role)
            _print({"role": args.role, "granted": "SELECT"})
            return 0
        expected = DatabaseFingerprint.model_validate_json(args.expected.read_bytes())
        journals: tuple[JournalFingerprint, JournalFingerprint] | None = None
        if args.journal_source and args.journal_restored:
            journals = (
                journal_fingerprint(args.journal_source),
                journal_fingerprint(args.journal_restored),
            )
        report = compare(
            expected,
            database_fingerprint(engine, now),
            journal=journals,
            objectives=Objectives(rto_seconds=args.rto_seconds, rpo_seconds=args.rpo_seconds),
            measured_restore_seconds=args.measured_restore_seconds,
            measured_data_loss_seconds=args.measured_data_loss_seconds,
        )
        _print(report.model_dump(mode="json"))
        return 0 if report.status == "PASS" else 1
    finally:
        engine.dispose()


def incident_command(args: argparse.Namespace) -> int:
    if args.command == "verify-incident":
        problems = verify_bundle(args.directory)
        _print({"problems": list(problems)})
        return 1 if problems else 0
    settings = Settings()
    engine = build_engine(settings.database_url)
    governance = GovernanceStore(args.database, resolve_principal(args.access_file))
    try:
        record, _ = open_incident(
            root=settings.artifact_root,
            store=PostgresDecisionStore(engine),
            governance=governance,
            category=Category(args.category),
            severity=args.severity,
            summary=args.summary,
            opened_at=datetime.now(UTC),
            window_start=args.window_start,
            window_end=args.window_end,
            source_ids=args.source,
            bookmakers=args.bookmaker,
            stop_sources=args.stop_sources,
            global_stop=args.global_stop,
        )
    finally:
        governance.close()
        engine.dispose()
    # The bundle path can name a local directory, so print only the incident ID.
    _print(
        {
            "incident_id": str(record.incident_id),
            "affected": len(record.affected_recommendation_ids),
            "stops": [stop.model_dump(mode="json") for stop in record.stops],
        }
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "evaluate-alerts":
            return evaluate_alerts(args)
        if args.command in ("open-incident", "verify-incident"):
            return incident_command(args)
        if args.command == "scheduler":
            return scheduler_command(args)
        return recovery_command(args)
    except ValidationError as error:
        # Do not echo input values; they can hold operational data.
        print(json.dumps({"error": "INVALID_INPUT", "fields": [e["loc"] for e in error.errors()]}))
        return 2
    except (OSError, ValueError, PermissionError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
