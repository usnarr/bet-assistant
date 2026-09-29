# SYS-03 — Immutable ingestion and replay evidence

Date: 2026-09-19. Scope: F03 synthetic/local verification.

## Result

Engineering result: **PASS** for the checked-in synthetic contracts and fault fixtures.
Production-source rollout: **BLOCKED** pending an F01-approved live source, permitted
recording, owner review, and prospective operational evidence.

Implemented evidence includes:

- distinct observations over one deduplicated immutable content object;
- failed/cached reads that cannot refresh observation freshness;
- hash verification before archive completion and parsing;
- staged-write recovery, missing/corrupt failure, and orphan recording;
- strict schema rejection, unexpected-empty rejection, and append-only dead letters;
- idempotent derived rows across command/replay retries and distinct parser versions;
- dry-run and source/resource/time replay filters;
- bounded transient retries, conditional requests, URL credential redaction, quotas, and
  concurrency controls;
- no retry loop on `403`, CAPTCHA, or `429`, with persistent runtime stop/suspension state;
- a PostgreSQL migration and optional isolated-service lineage/append-only integration test.

Fixture: `tests/fixtures/sources/synthetic-sports-v1/`. Its manifest labels it synthetic
and records that independent review is pending. It contains no bookmaker quote, provider
record, credential, or claimed real policy.

## Verification record

Run on 2026-09-19 from the locked environment:

- `uv sync --frozen --offline`: passed; editable package rebuilt from the lock.
- `uv run ruff format --check .`: 51 files formatted.
- `uv run ruff check .`: passed.
- `uv run mypy src`: passed for 35 source files.
- `uv run pytest -q`: 106 passed, 3 integration checks skipped without service variables.
- isolated PostgreSQL 17.6 and MinIO integration run: 3 passed, including migration
  upgrade/downgrade/re-upgrade, append-only history, persisted F03 lineage, and immutable
  object round trip; disposable containers were stopped and auto-removed afterward.
- `uv run python scripts/check_migrations.py`: one head at `0003_ingestion`; full PostgreSQL
  DDL compiled offline.
- `uv run pip-audit`: no known third-party dependency vulnerabilities; the local
  `tennis-engine` package was correctly reported as not present on PyPI.

The suite emitted two existing framework deprecation warnings during the local run and one
pytest cache-permission warning in the elevated isolated-service run; none changed test
outcomes. Network-dependent live-source evaluation was intentionally not run. Independent
fixture review, approved live-source collection, metrics/alert integration, and a sustained
operational replay report remain open gates.
