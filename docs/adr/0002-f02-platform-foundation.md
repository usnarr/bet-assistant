# ADR 0002 — F02 platform foundation

Date: 2026-09-19. Status: implemented for local and CI use.

F02 establishes shared contracts and a repeatable service boundary before ingestion,
modelling, or recommendations are added. Python 3.13 and `uv` remain the runtime and
dependency tools. FastAPI provides the internal HTTP boundary, SQLAlchemy plus Alembic
owns PostgreSQL schema changes, and MinIO supplies the local S3-compatible object store.
The API and worker fail readiness when PostgreSQL is unavailable, its migration revision
is incompatible, or the configured object-store bucket is missing.

The repository selects Polars for future dataframe transformations and Prefect for future
job orchestration. They are architectural choices, not F02 runtime dependencies: each is
added to the lockfile when F03/F07 supplies a concrete consumer and contract tests. This
keeps the platform image small and avoids exposing a scheduler before jobs exist. Redis,
MLflow, Prometheus, and Grafana follow the same consumer-driven rule.

Domain contracts reject naive timestamps and binary floats for probabilities, odds, and
money. Money is represented by `Decimal` plus an explicit PLN currency and becomes a JSON
string. A signed monetary value is allowed because profit and expected value can be
negative; stake and return contracts enforce non-negative cash flows. Identifiers are
opaque UUIDs, and deterministic UUIDv5 identifiers are limited to reproducible fixtures
and natural-key adapters.

The first PostgreSQL migrations create a `tennis` schema, immutable artifact manifests,
append-only governance revisions, evidence metadata, stop history, and audit events. Raw
evidence bytes belong in object storage; PostgreSQL stores hashes and object keys. The
migration inserts a global disabled state so a new database cannot publish recommendations.
The existing F01 SQLite store remains the offline administrative reference during the
transition; F03 will route new ingestion through PostgreSQL and object storage.

Artifact manifests use canonical JSON and hashes for datasets, models, evaluation runs,
and agent traces. Reusing an artifact ID with different manifest bytes fails. Secrets are
typed, redacted from logs and configuration output, and supplied through the environment.
Production configuration refuses placeholder credentials or non-TLS object storage.

Migrations are forward and backward tested against an isolated PostgreSQL service in CI.
The local unit suite also checks the head graph and compiles PostgreSQL SQL offline. A
failed required dependency or schema mismatch yields false readiness; liveness remains
available so an operator can distinguish a running process from a usable service.
