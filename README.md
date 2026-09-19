# Tennis Betting AI Engine

F01 governance and F02 platform foundation are implemented. The project now includes
source approvals, versioned payout/responsible-use contracts, strict shared domain
contracts, PostgreSQL migrations, immutable S3-compatible storage, an internal health API,
a dependency-gated worker, artifact manifests, and CI. Prediction, ingestion, pricing, and
the dashboard remain future work.

```powershell
uv sync --frozen
uv run tennis-governance init
uv run tennis-governance check-source betclic-odds production
uv run tennis-platform show-config
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The source check returns exit code **2** and a denial until reviewed configuration
has been applied. Initializing a database grants no approvals or operator roles.

See the [F01 operator guide](docs/governance/README.md),
[F02 platform guide](docs/platform/README.md),
[provider comparison](docs/governance/provider-comparison.md),
[architecture decisions](docs/adr/0001-f01-governance.md), and
[evaluation evidence](docs/governance/evidence/SYS-01.md).

On a restricted Windows host, use workspace-local runtime/cache/temp directories:

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location) '.python'
uv python install 3.13 --no-bin
New-Item -ItemType Directory -Force var/test-temp | Out-Null
$env:TMPDIR = (Resolve-Path var/test-temp).Path
uv sync --frozen
uv run pytest -q
```

No paid provider subscriptions or live bookmaker credentials are needed by the tests.
See the F02 guide for the optional local PostgreSQL/MinIO Compose smoke path.
