"""Local platform administration commands."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from minio import Minio

from tennis_engine.governance.contracts import Role

from .health import InfrastructureProbe, public_checks, ready
from .settings import Settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="tennis-platform")
    subcommands = result.add_subparsers(dest="command", required=True)
    subcommands.add_parser("health", help="Check required database and object-store dependencies")
    subcommands.add_parser(
        "init-object-store", help="Create the configured local bucket if missing"
    )
    subcommands.add_parser("show-config", help="Show non-secret effective configuration")
    issue = subcommands.add_parser(
        "create-api-token", help="Add an F14 API token; print the plain token once"
    )
    issue.add_argument("--identity", required=True)
    issue.add_argument("--role", required=True, choices=[role.value for role in Role])
    issue.add_argument("--rotate", action="store_true", help="Replace the identity's token")
    issue.add_argument("--file", type=Path, help="Default: TENNIS_API_CREDENTIALS_FILE")
    revoke = subcommands.add_parser("revoke-api-token", help="Remove an F14 API token")
    revoke.add_argument("--identity", required=True)
    revoke.add_argument("--file", type=Path, help="Default: TENNIS_API_CREDENTIALS_FILE")
    return result


def _credentials_file(args: argparse.Namespace, settings: Settings) -> Path:
    path: Path | None = args.file or settings.api_credentials_file
    if path is None:
        raise ValueError("Set --file or TENNIS_API_CREDENTIALS_FILE")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = Settings()
        if args.command == "health":
            checks = InfrastructureProbe(settings).check()
            print(json.dumps({"ready": ready(checks), "dependencies": public_checks(checks)}))
            return 0 if ready(checks) else 2
        if args.command == "show-config":
            print(json.dumps(settings.public_summary(), sort_keys=True))
            return 0
        if args.command in ("create-api-token", "revoke-api-token"):
            from tennis_engine.serving.auth import issue_token, revoke_token

            path = _credentials_file(args, settings)
            if args.command == "revoke-api-token":
                removed = revoke_token(path, args.identity)
                print(json.dumps({"identity": args.identity, "revoked": removed}))
                return 0 if removed else 2
            token = issue_token(path, args.identity, Role(args.role), rotate=args.rotate)
            # The plain token appears only here. The file keeps its SHA-256 digest.
            print(json.dumps({"identity": args.identity, "role": args.role, "token": token}))
            return 0
        client = Minio(
            settings.object_store_endpoint,
            access_key=settings.object_store_access_key.get_secret_value(),
            secret_key=settings.object_store_secret_key.get_secret_value(),
            secure=settings.object_store_secure,
        )
        created = not client.bucket_exists(settings.object_store_bucket)
        if created:
            client.make_bucket(settings.object_store_bucket)
        print(json.dumps({"bucket": settings.object_store_bucket, "created": created}))
        return 0
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
