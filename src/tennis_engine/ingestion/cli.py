"""F03 operator commands for approved fixture import, replay, and reconciliation."""

import argparse
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from minio import Minio

from tennis_engine.common.clock import SystemClock, require_aware
from tennis_engine.governance.cli import resolve_principal
from tennis_engine.governance.contracts import Purpose
from tennis_engine.governance.service import GovernanceService
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.database import build_engine
from tennis_engine.infrastructure.object_store import S3ObjectStore
from tennis_engine.infrastructure.settings import Settings

from .contracts import ReplayRequest
from .fetchers import ApprovedFileFetcher
from .parser import ParserRegistry
from .postgres import PostgresIngestionStore
from .service import IngestionService
from .synthetic import SyntheticSportsParser


def _timestamp(value: str) -> datetime:
    try:
        return require_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="F03 immutable ingestion and replay")
    commands = root.add_subparsers(dest="command", required=True)
    archive = commands.add_parser(
        "archive-file", help="Import one policy-approved file and parse it after archiving"
    )
    archive.add_argument("file", type=Path)
    archive.add_argument("--allowed-root", type=Path, required=True)
    archive.add_argument("--source", required=True)
    archive.add_argument("--resource-id", required=True)
    archive.add_argument("--idempotency-key", required=True)
    archive.add_argument("--observation-window", type=_timestamp, required=True)
    archive.add_argument("--parser-version", default=SyntheticSportsParser.version)
    archive.add_argument("--purpose", choices=list(Purpose), default=Purpose.PROTOTYPE)
    archive.add_argument("--governance-database", type=Path, default=Path("var/governance.sqlite3"))
    archive.add_argument(
        "--governance-access-file", type=Path, default=Path("var/governance-access.json")
    )
    replay = commands.add_parser("replay", help="Re-parse selected immutable observations")
    replay.add_argument("--source")
    replay.add_argument("--event-id", dest="logical_resource_id")
    replay.add_argument("--from", dest="observed_from", type=_timestamp)
    replay.add_argument("--to", dest="observed_to", type=_timestamp)
    replay.add_argument("--parser-version", required=True)
    replay.add_argument("--dry-run", action="store_true")
    commands.add_parser("reconcile", help="Verify staged/missing/corrupt/orphan raw objects")
    return root


def _services() -> tuple[IngestionService, ParserRegistry]:
    settings = Settings()
    client = Minio(
        settings.object_store_endpoint,
        access_key=settings.object_store_access_key.get_secret_value(),
        secret_key=settings.object_store_secret_key.get_secret_value(),
        secure=settings.object_store_secure,
    )
    service = IngestionService(
        PostgresIngestionStore(build_engine(settings.database_url)),
        S3ObjectStore(client, settings.object_store_bucket),
        SystemClock(),
    )
    return service, ParserRegistry((SyntheticSportsParser(),))


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    governance: GovernanceStore | None = None
    try:
        service, registry = _services()
        if args.command == "reconcile":
            result: object = service.reconcile()
        elif args.command == "replay":
            result = service.replay(
                ReplayRequest(
                    source_id=args.source,
                    logical_resource_id=args.logical_resource_id,
                    observed_from=args.observed_from,
                    observed_to=args.observed_to,
                    parser_version=args.parser_version,
                    dry_run=args.dry_run,
                ),
                registry,
            ).model_dump(mode="json")
        else:
            parser_instance = registry.get(args.parser_version)
            governance = GovernanceStore(
                args.governance_database,
                resolve_principal(args.governance_access_file),
            )
            guard = GovernanceService(governance)
            capture = ApprovedFileFetcher(
                args.allowed_root,
                guard,
                SystemClock(),
                purpose=Purpose(args.purpose),
            ).fetch(
                args.file,
                source_id=args.source,
                logical_resource_id=args.resource_id,
            )
            decision = guard.can_fetch(args.source, Purpose(args.purpose))
            if not decision.allowed or decision.version is None or decision.revision is None:
                raise PermissionError(decision.reason)
            archived = service.archive(
                idempotency_key=args.idempotency_key,
                observation_window=args.observation_window,
                capture=capture,
                parser_candidate=parser_instance.version,
                policy_version=decision.version,
                policy_revision=decision.revision,
            )
            if archived.observation_id is None:
                raise RuntimeError("Successful file import did not create an observation")
            parsed = service.parse_observation(
                service.repository.observation(archived.observation_id), parser_instance
            )
            result = {
                "fetch": archived.model_dump(mode="json"),
                "parse": parsed.model_dump(mode="json"),
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        return 2
    finally:
        if governance is not None:
            governance.close()


if __name__ == "__main__":
    raise SystemExit(main())
