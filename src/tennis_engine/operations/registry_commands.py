"""`tennis-ops registry`: register bundles, switch the champion, roll back (F11.8, F13.9).

The actor comes from the local access file (see the F01 guide). A switch needs the
`policy_reviewer` role; a rollback also allows `operator`. Output holds IDs and codes,
never a local path.
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from tennis_engine.backtesting.promotion import ReleaseDecision
from tennis_engine.common.clock import SystemClock
from tennis_engine.features.core import CORE_SET
from tennis_engine.governance.cli import resolve_principal
from tennis_engine.infrastructure.database import build_engine
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.models.registry import NO_CHAMPION, ModelRegistry, build_bundle, verify_bundle
from tennis_engine.models.registry_postgres import PostgresRegistryStore

# Feature sets that this code revision can compute. A bundle on another set cannot load.
RUNTIME_FEATURE_SETS = frozenset({CORE_SET.sha256})


def add_parser(commands: Any) -> None:
    registry = commands.add_parser("registry", help="F11.8 model registry and champion switch")
    registry.add_argument("--access-file", type=Path, default=Path("var/governance-access.json"))
    registry.add_argument(
        "--runtime-lock", type=Path, default=Path("uv.lock"), help="Lock file of this runtime"
    )
    actions = registry.add_subparsers(dest="registry_command", required=True)
    register = actions.add_parser("register", help="Register a stored model bundle")
    register.add_argument("--family", required=True)
    register.add_argument("--version", required=True)
    register.add_argument("--model-dir", type=Path, required=True)
    register.add_argument("--calibrator-dir", type=Path)
    register.add_argument("--evaluation-report", type=Path, required=True)
    register.add_argument("--rollback-target", type=UUID)
    promote = actions.add_parser("promote", help="Audited champion switch (PASS decision only)")
    promote.add_argument("--bundle", type=UUID, required=True)
    promote.add_argument("--decision", type=Path, required=True, help="release-decision.json")
    promote.add_argument("--reason", required=True)
    rollback = actions.add_parser("rollback", help="Restore a complete earlier bundle")
    rollback.add_argument("--family", required=True)
    rollback.add_argument("--reason", required=True)
    rollback.add_argument(
        "--target", default="declared", help="declared, no-champion or a bundle ID"
    )
    show = actions.add_parser("show", help="The champion and the event history")
    show.add_argument("--family", required=True)
    verify = actions.add_parser("verify", help="Check every file hash of one bundle")
    verify.add_argument("--bundle", type=UUID, required=True)


def _lock(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _target(value: str) -> UUID | Literal["declared", "no-champion"]:
    if value == "declared":
        return "declared"
    if value == NO_CHAMPION:
        return "no-champion"
    return UUID(value)


def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    engine = build_engine(settings.database_url)
    registry = ModelRegistry(PostgresRegistryStore(engine), settings.artifact_root, SystemClock())
    lock = _lock(args.runtime_lock)
    try:
        command = args.registry_command
        if command == "register":
            actor = resolve_principal(args.access_file)
            bundle = build_bundle(
                settings.artifact_root,
                family=args.family,
                version=args.version,
                model_dir=args.model_dir,
                calibrator_dir=args.calibrator_dir,
                evaluation_report=args.evaluation_report,
                rollback_target=args.rollback_target,
                registered_by=actor.identity.lower(),
                registered_at=SystemClock().now(),
            )
            created = registry.register(bundle)
            return {"bundle_id": str(bundle.bundle_id), "created": created}
        if command == "promote":
            decision = ReleaseDecision.model_validate_json(args.decision.read_bytes())
            event = registry.promote(
                args.bundle,
                decision,
                actor=resolve_principal(args.access_file),
                reason=args.reason,
                supported_feature_sets=RUNTIME_FEATURE_SETS,
                runtime_lock_sha256=lock,
            )
            return event.model_dump(mode="json")
        if command == "rollback":
            event = registry.rollback(
                args.family,
                actor=resolve_principal(args.access_file),
                reason=args.reason,
                supported_feature_sets=RUNTIME_FEATURE_SETS,
                target=_target(args.target),
                runtime_lock_sha256=lock,
            )
            return event.model_dump(mode="json")
        if command == "show":
            champion = registry.champion(args.family)
            return {
                "champion": str(champion.bundle_id) if champion else None,
                "events": [
                    json.loads(item.model_dump_json())
                    for item in registry.store.events(args.family)
                ],
            }
        stored = registry.store.bundle(args.bundle)
        if stored is None:
            return {"bundle_id": str(args.bundle), "problems": ["BUNDLE_UNKNOWN"]}
        bundle = stored
        return {
            "bundle_id": str(bundle.bundle_id),
            "problems": list(verify_bundle(settings.artifact_root, bundle)),
        }
    finally:
        engine.dispose()
