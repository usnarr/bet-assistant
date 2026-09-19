"""Local platform administration commands."""

import argparse
import json
from collections.abc import Sequence

from minio import Minio

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
    return result


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
