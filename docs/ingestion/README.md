# F03 ingestion and replay operator guide

F03 archives source bytes before transformation, keeps observations separate from shared
content, validates parser outputs, preserves rejected payloads, and supports filtered
replay without rewriting history. It does not provide a production sports or bookmaker
adapter; those begin in F04/F05 after source approval.

## Durable storage

- PostgreSQL stores commands, fetch attempts, observations, archive transitions, parsing
  attempts, derived versions, dead-letter revisions, replay jobs, runtime stops, and orphan
  evidence.
- MinIO/S3 stores immutable gzip payloads under
  `raw/<source>/<hash-prefix>/<uncompressed-sha256>.<json|bin>.gz`.
- Redis and vector storage are not runtime requirements. Any future cache or semantic index
  is derived and rebuildable, never the source of financial or safety truth.

The database schema revision is `0003_ingestion`. API/worker readiness remains false until
PostgreSQL is on that exact revision and the configured object-store bucket exists.

## Commands

After applying migrations and creating the object-store bucket:

```powershell
uv run alembic upgrade head
uv run tennis-platform init-object-store
uv run tennis-ingestion reconcile
uv run tennis-ingestion replay --parser-version synthetic-sports-v1 --dry-run
```

Replay filters are `--source`, `--event-id`, `--from`, and `--to`; time intervals are
`[from, to)`. The bundled parser and payload are synthetic SYS-03 fixtures only.

`archive-file` also requires a current F01-approved file source, an allowed filesystem root,
an explicit idempotency key, and an observation window. It fails closed when the policy is
missing, suspended, expired, or killed. Do not approve a real source merely to exercise the
command; use the unit fixture or a separately reviewed prototype source.

## Failure and recovery behavior

- Failed HTTP attempts can retain response bytes but never create a fresh observation.
- Schema drift, malformed or unexpectedly empty parser output creates a dead-letter
  revision and no derived output.
- `401`, `403`, CAPTCHA, and persisted access-stop state require operator/policy review;
  workers do not rotate identities or auto-resume.
- `429` stores a bounded suspension based on `Retry-After`; transient network/selected
  `5xx` retries remain bounded.
- Missing or corrupt archives block parsing. `reconcile` verifies hashes, recovers staged
  writes, marks failures, and records orphan keys without deleting them.

Monitor attempt status, last successful observation, bytes archived, parsing rejection
rate, dead-letter age, runtime stops, replay counts, and reconciliation failures. F15 will
connect these records to metrics and alerts.

## Validation

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python scripts/check_migrations.py
```

The PostgreSQL/MinIO tests remain opt-in and require isolated `TEST_*` endpoints. Never
point them at production storage. See [SYS-03 evidence](evidence/SYS-03.md).
