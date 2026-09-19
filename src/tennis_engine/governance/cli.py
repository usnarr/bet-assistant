"""Controlled local policy commands. See docs/governance/README.md for trust boundaries."""

import argparse
import getpass
import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from .contracts import (
    PayoutSchedule,
    Principal,
    Purpose,
    ResponsibleUseSchedule,
    Role,
    SourcePolicy,
)
from .retention import RetentionService
from .service import GovernanceService
from .store import GovernanceStore


def resolve_principal(access_file: Path) -> Principal:
    """Local OS account mapping; remote callers need a separate trusted authenticator."""
    identity = getpass.getuser()
    roles = json.loads(access_file.read_text(encoding="utf-8")) if access_file.exists() else {}
    return Principal(identity=identity, role=Role(roles.get(identity, "dashboard")))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="F01 local governance journal")
    root.add_argument("--database", type=Path, default=Path("var/governance.sqlite3"))
    root.add_argument("--access-file", type=Path, default=Path("var/governance-access.json"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create an empty store; all permissions default to denied")
    commands.add_parser("export", help="Export the audit journal without raw document bytes")
    apply = commands.add_parser("apply", help="Validate and append one policy document")
    apply.add_argument("kind", choices=["source", "payout", "responsible_use"])
    apply.add_argument("file", type=Path)
    apply.add_argument("--expected-revision", type=int, required=True)
    apply.add_argument("--reason", required=True)
    check = commands.add_parser("check-source")
    check.add_argument("source_id")
    check.add_argument("purpose", choices=list(Purpose))
    archive = commands.add_parser(
        "archive-document", help="Archive supplied permitted bytes for review"
    )
    archive.add_argument("document_id")
    archive.add_argument("scope", help="source:<id>, payout:<bookmaker>, responsible_use:<scope>")
    archive.add_argument("file", type=Path)
    archive.add_argument("--reference", required=True)
    archive.add_argument("--reason", required=True)
    disable = commands.add_parser("global-disable")
    disable.add_argument("state", choices=["on", "off"])
    disable.add_argument("--reason", required=True)
    commands.add_parser("expire-raw")
    return root


def main() -> int:
    args = parser().parse_args()
    store = None
    try:
        args.database.parent.mkdir(parents=True, exist_ok=True)
        store = GovernanceStore(args.database, resolve_principal(args.access_file))
        result: object
        if args.command == "init":
            result = {"schema_version": 1, "global_disable": store.global_disabled(store.clock())}
        elif args.command == "export":
            result = store.export()
        elif args.command == "apply":
            models: dict[
                str, type[SourcePolicy] | type[PayoutSchedule] | type[ResponsibleUseSchedule]
            ] = {
                "source": SourcePolicy,
                "payout": PayoutSchedule,
                "responsible_use": ResponsibleUseSchedule,
            }
            document = models[args.kind].model_validate_json(args.file.read_bytes())
            revision = store.save(
                document, expected_revision=args.expected_revision, reason=args.reason
            )
            result = {"revision": revision}
        elif args.command == "archive-document":
            revision, sha256 = store.archive_document(
                args.document_id,
                args.scope,
                args.reference,
                args.file.read_bytes(),
                reason=args.reason,
            )
            result = {"revision": revision, "sha256": sha256, "review_status": "PENDING_REVIEW"}
        elif args.command == "global-disable":
            result = {"revision": store.set_global_disable(args.state == "on", reason=args.reason)}
        elif args.command == "expire-raw":
            result = {"deleted_objects": RetentionService(store).expire_due()}
        else:
            decision = GovernanceService(store).can_fetch(args.source_id, Purpose(args.purpose))
            print(decision.model_dump_json(indent=2))
            return 0 if decision.allowed else 2
        print(json.dumps(result, indent=2))
        return 0
    except ValidationError as error:
        # Avoid reflecting arbitrary policy values/secrets from failed inputs.
        print(json.dumps({"error": "INVALID_POLICY", "fields": [e["loc"] for e in error.errors()]}))
        return 2
    except (ValueError, PermissionError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
