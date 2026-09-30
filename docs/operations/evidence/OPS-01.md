# OPS-01 evidence — failure injection, controls and security fixtures

Date: 2026-09-30. Scope: synthetic fixtures, an isolated PostgreSQL 17.6 container, an
isolated SeaweedFS 4.47 container and temporary F01 journals. No real source, quote,
account or production store was used.

## Local run

| Check | Result |
|---|---|
| `uv sync --frozen` | pass |
| `uv run ruff format --check .` | pass (210 files) |
| `uv run ruff check .` | pass |
| `uv run mypy src` | pass (139 source files) |
| `uv run pytest -q` with isolated PostgreSQL and SeaweedFS | 612 passed, 0 skipped |
| F14 wiring and F15 tests (7 files, unit and integration) | 46 passed |
| `uv run python scripts/check_migrations.py` | pass (single head `0011_operations`) |
| `uv run pip-audit` | no known vulnerabilities; no dependency added |
| Trivy 0.66.0 on the F14 image (CRITICAL, HIGH, `--ignore-unfixed`) | 0 findings; the Dockerfile did not change after that scan |

The integration tests run `alembic downgrade base`, `upgrade head`, and a downgrade to
`0010_decision_records` and back, on the isolated database.

## Failure and recovery drills (F15.1, F15.2)

Each drill runs on the in-memory stores (`tests/test_operations_jobs.py`) and on
PostgreSQL (`tests/integration/test_operations_persistence.py`). The shared drill is
`tests/operations_support.py::drills`.

| Drill | Expected | Result |
|---|---|---|
| Duplicate delivery | Same run; `ALREADY_SUCCEEDED`; the effect runs once | pass |
| Scheduler restart (new runner, same store) | Same as duplicate delivery | pass |
| Incomplete identity input before publication | `BLOCKED`, reason `INPUT:identity:INCOMPLETE`, no effect | pass |
| Missing upstream job status | `BLOCKED`, reason `DEPENDENCY:evaluate_quotes:INCOMPLETE` | pass |
| Worker crash (exception) | `FAILED` attempt, lease released, retry `SUCCEEDED` | pass |
| Repeated crashes | `EXHAUSTED` after `max_attempts` | pass |
| Stale lock: worker A stalls past its lease, worker B takes over | B succeeds with a larger token; A's success is refused (`LEASE_LOST`); one success | pass |
| Old lease renews or reacquires a held resource | `LeaseLost`; acquire returns none | pass |
| Backfill pool full | Backfill `BUSY`; prospective job still `SUCCEEDED` | pass |
| Direct second `SUCCEEDED` insert (PostgreSQL) | Unique index rejects it | pass |
| Update or delete job history (PostgreSQL) | Trigger rejects it (`append-only`) | pass |
| Lower a fencing token (PostgreSQL) | Trigger rejects it | pass |

Source outage and partial storage write: F14 read checks serve a record from a stale or
disabled source as `NO_BET` (SYS-12). A failed decision store gives HTTP 503
`DEPENDENCY_UNAVAILABLE` (`test_production_app_starts_with_valid_settings_and_fails_closed_on_the_store`).
A failed or missing journal gives `NO_BET` with `READ_CHECK_UNAVAILABLE`
(`test_missing_journal_serves_no_bet_and_fails_readiness`). F03 tests cover partial raw
writes: `test_storage_failure_keeps_pending_evidence_and_blocks_checkpoint`,
`test_reconciliation_recovers_bytes_written_after_staging` and
`test_corrupt_archive_blocks_parsing_and_reconciliation_records_orphans`.

## Controlled alert test (F15.4)

`tests/test_monitoring.py`:

| Case | Result |
|---|---|
| Healthy synthetic signals raise no alert on the proposed rule set | pass |
| `parser_events = 0` raises `parser-zero-events` (CRITICAL, `SOURCE_STOP`) | pass |
| An expected source without signals raises `TELEMETRY_MISSING` | pass |
| A signal older than `max_signal_age_seconds` is missing telemetry | pass |
| Missing global leakage evidence raises the alert without a control | pass |
| Applying the source stop: `can_fetch` gives `SOURCE_STOPPED`; the next F14 read serves the BET as `NO_BET` | pass |
| Applying the same alerts again appends nothing | pass |
| An operator cannot resume; a reviewer can | pass |
| `settlement_mismatch_count = 1` turns the global stop on | pass |
| Warnings never apply a control | pass |
| `tennis-ops evaluate-alerts --apply` (exit 1 with alerts, 0 without, 2 on invalid input without echoing values) | pass |

The control takes effect on the next read. F14 has no cache: every current read rechecks
the journal (`test_every_current_read_rechecks_and_nothing_is_cached`, SYS-12).

## Security fixtures (F15.5)

| Case | Test | Result |
|---|---|---|
| Viewer and agent roles cannot read metrics or audit | `test_metrics_need_an_internal_role_and_hold_no_token_or_raw_ids`, SYS-12 | pass |
| No F14 route writes | `test_the_api_has_no_write_route` (SYS-12) | pass |
| Metrics hold no token and no record ID | same metrics test | pass |
| Settings summary hides secrets and paths | `test_public_summary_hides_paths_and_secrets` | pass |
| Production refuses placeholder database passwords (4 cases) | `test_production_refuses_placeholder_or_missing_database_passwords` | pass |
| Production refuses missing, empty, invalid and placeholder credentials, and a missing or uninitialized journal | `test_production_refuses_unsafe_serving_configuration` | pass |
| The API journal connection rejects writes | `test_read_only_journal_rejects_writes` | pass |
| The reader role can read but cannot delete, update, insert or create | `test_reader_role_can_read_but_not_write_and_serves_the_api` | pass |
| The API works through the reader role | same | pass |
| Token commands store digests only | `test_token_commands_store_digests_only` | pass |
| Artifact substitution: a changed journal row, incident file or model hash is detected | `test_journal_backup_is_identical_and_tampering_is_detected`, `test_incident_stops_first_and_preserves_evidence`, `test_model_artifacts_load_only_with_a_matching_hash` | pass |
| No unsafe deserialization or `shell=True` in `src/` | `test_source_has_no_unsafe_deserialization_or_shell_execution` | pass |

Cross-role agent calls: F18 agents do not exist. The agent token role has the same denials
as the dashboard role. Agent tool scoping (F15.6) is pending.

## Compose smoke test

Stack on alternate host ports with a Compose override file, project `f15-smoke`:

| Check | Result |
|---|---|
| `governance-init`, `migrate`, `object-store-init` complete; `api` healthy | pass |
| `/health/ready` | 200; database `0010_decision_records`, `governance_journal` `read-only; global_stop=on`, object store ready |
| F14 route without a token | 401 |
| Token from `docker compose run --rm admin tennis-platform create-api-token`, then API restart | 200; `responsible_use.reason` `GLOBAL_DISABLE`; 0 records |
| Write to the journal mount, the credential mount and `/app` inside the API container | refused (read-only file system) |

The smoke test ran at the F14 wiring commit, before migration `0011_operations`. The
stack was brought down with its volumes.

## Limits

- All data is synthetic. The alert thresholds are proposals.
- The drills run in one process on one machine. They do not prove behaviour under real
  network partitions or clock skew between hosts. Lease expiry uses the caller's clock.
- No scheduler runs the jobs yet.
