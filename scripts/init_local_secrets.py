"""Create random local secret files for the Compose stack. Development use only.

Usage:

    uv run python scripts/init_local_secrets.py            # into ./secrets
    uv run python scripts/init_local_secrets.py --directory <dir>

An existing file is kept, so the command is safe to run again. The directory is private
(0700 on POSIX). Each file is readable by the container user (0644), because a Compose
file secret is a bind mount of the file. The script prints only file names, never a
value. The `secrets/` directory is ignored by Git and Docker. Production secrets come
from the host's secret manager, not from this script.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

# File name -> number of random bytes. Tokens and passwords are URL-safe text.
SECRETS: dict[str, int] = {
    "prometheus-api-token": 32,
    "grafana-admin-password": 24,
}


def create(directory: Path) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    created = []
    for name, size in SECRETS.items():
        target = directory / name
        if target.exists():
            continue
        target.write_text(secrets.token_urlsafe(size) + "\n", encoding="utf-8")
        if os.name == "posix":
            target.chmod(0o644)
        created.append(name)
    return created


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("secrets"))
    args = parser.parse_args()
    created = create(args.directory)
    print(f"created: {', '.join(created) if created else 'none'}")
    print(f"present: {len(SECRETS)} secret files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
