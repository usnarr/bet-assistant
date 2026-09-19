# F02 platform operator guide

F02 provides the shared contracts, PostgreSQL migrations, immutable object storage,
artifact manifests, internal health API, worker startup gate, and CI pipeline. It does not
collect external data, train models, publish recommendations, or place wagers.

The latest local verification record is [SYS-02](evidence/SYS-02.md).

## Local setup

Copy `.env.example` to `.env` and change the local passwords. The example contains only
development placeholders. Then run:

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location) '.python'
uv sync --frozen
docker compose up --build -d
docker compose ps
uv run tennis-platform health
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

`/health/live` confirms only that the API process runs. `/health/ready` returns HTTP 503
until PostgreSQL is reachable at migration `0002_governance` and the MinIO bucket exists.
The worker waits at the same gate. `migrate` and `object-store-init` complete before Compose
starts the API.

Stop services with `docker compose down`. This preserves named development volumes. Use a
dedicated Compose project or explicit test endpoints for integration tests; never point a
test at production storage.

## Validation

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python scripts/check_migrations.py
uv run pip-audit
```

Tests need no live credentials. PostgreSQL and MinIO integration tests run only when the
`TEST_DATABASE_URL` and `TEST_OBJECT_STORE_*` variables identify isolated services. CI
supplies both services and tests migration upgrade, downgrade, re-upgrade, append-only
history, and immutable object round trips.

## Migration rules

Create migrations in dependency order and keep a single Alembic head. Existing migration
files are immutable after release; corrections use a new revision. Prefer reversible DDL.
When reversal would lose business history, write and test a forward-recovery migration
instead. Readiness must reject a database whose revision differs from the application.

Append-only tables reject `UPDATE` and `DELETE`. Corrections append a replacement event
with lineage. Retention deletion removes permitted raw bytes from object storage and adds
an audit tombstone; it must not rewrite governance or audit history.

## Artifact convention

Each dataset, model, evaluation, or agent trace has an immutable `manifest.json` under
`var/artifacts/<kind>/<artifact-id>/`. The manifest records its schema, content hash, Git
revision, input component hashes, source revisions, and UTC creation time. Licensed or
sensitive artifacts belong in access-controlled storage; Git stores only safe evidence.
