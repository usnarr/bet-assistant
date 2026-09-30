# OPS-02 evidence — backup, restore and reconciliation drill

Date: 2026-09-30. Status: **integrity PASS; readiness BLOCKED** (RTO and RPO are not agreed).

Scope: a disposable PostgreSQL 17.6 container, a temporary F01 journal and a temporary
local object store. All data is synthetic. No production store was used.

## Command

```sh
TEST_DATABASE_URL=postgresql+psycopg://tennis:test-only@127.0.0.1:55442/tennis_test \
  MSYS_NO_PATHCONV=1 uv run python scripts/ops02_restore_drill.py --container f15-test-pg
```

## Bundle

`tests/recovery_support.py::seed` writes through the real services:

| Content | Count |
|---|---|
| Raw content (F03 archive, gzip object) | 1 |
| Stored decisions (BET, WATCH, NO_BET and 5 NO_BET copies) | 8 |
| Virtual ledger with 2 virtual bets (F06) | 1 ledger |
| Job run with a fenced success (F15) | 1 |
| Journal records (global stop, source stop) | 2 |

The database fingerprint covered 50 tables and 34 rows at revision `0011_operations`.

## Steps and results

| Step | Tool | Result |
|---|---|---|
| Fingerprint the source | `database_fingerprint` | 50 tables |
| Back up the database | `pg_dump -Fc` in the container | done |
| Back up the journal | SQLite online backup (`backup_journal`) | done |
| Back up raw objects | directory copy | done |
| Backup duration | | 0.247 s |
| Restore into a new database | `createdb`, `pg_restore` | done |
| Compare every table (row count and ordered-row MD5) | `compare` | no difference |
| Compare the Alembic revision | `compare` | `0011_operations` both |
| Compare journals (row hashes and chain) | `journal_fingerprint` | equal, no problem |
| Raw objects against the restored lineage | `reconcile_raw_objects` | 1 of 1 verified |
| Ledger reconciliation on the restored database | `reconcile_ledgers` | 1 ledger balanced, closing 80.00 PLN |
| Restore and verification duration | | 1.079 s |
| Data-loss window | | 0.0 s (no write after the backup) |

Report: `integrity: PASS`, `status: BLOCKED`, finding `OBJECTIVES_UNSET`.

## Interpretation

- The restore reproduced every table exactly, and the lineage from raw content to the
  object store and the ledger balance survived the restore.
- The measured times are for a tiny synthetic bundle on one development machine. They are
  not an estimate of production recovery time.
- The data-loss window is zero only because nothing wrote between the backup and the
  failure point. A real RPO depends on the backup schedule. `pg_dump` is a point-in-time
  copy; continuous WAL archiving is not configured.
- Operational readiness stays blocked until the owner agrees RTO and RPO. Then rerun with
  `--rto-seconds` and `--rpo-seconds`.

## Negative controls

`tests/integration/test_recovery_persistence.py` and `tests/test_operations_recovery.py`:

| Change | Detected as |
|---|---|
| One added decision row | `TABLE:decision_record` |
| A deleted raw object | `OBJECT_MISSING:<content_id>` |
| A changed journal payload in the backup | `JOURNAL_HASH_MISMATCH:<revision>` and `JOURNAL:MISMATCH` |
| Restore slower than the RTO | `OBJECTIVES_NOT_MET` |

## Model rollback drill (F11.8, F13.9)

Date: 2026-09-30. Status: **PASS**. Scope: an isolated PostgreSQL 17.6 test container
and a temporary artifact root. Two synthetic bundles (F09 ranking baseline, F11 Platt
calibrator, model card, evaluation report) trained on the fictional history of the tests.

```sh
TEST_DATABASE_URL=postgresql+psycopg://tennis:test-only@127.0.0.1:55444/tennis_test \
  uv run python scripts/ops02_rollback_drill.py
```

| Step | Result |
|---|---|
| Real F13 decision on the synthetic walk-forward run, committed release configuration | `BLOCKED`; switch refused with `DECISION_NOT_PASS:BLOCKED` and the non-passing gates |
| Champion after the refusal | none |
| Mixed bundle (model v1 with calibrator v2) | refused, `CALIBRATOR_BELONGS_TO_ANOTHER_MODEL` |
| Decision approved by its author | refused, `REVIEWER_IS_AUTHOR` |
| Switch by the author | refused, `AUTHOR_CANNOT_SWITCH` |
| Promote v1, then v2, with PASS decision fixtures (drill only, not model evidence) | done |
| Rollback by an operator to the declared target v1, then load with every hash checked | v1 active; calibrator base hash equals the artifact hash; 0.022 s |
| Promote v2 again, change one byte of the v1 calibrator, roll back | refused, `ROLLBACK_TARGET:HASH_MISMATCH:calibrator`; v2 stays champion |
| Restore the file from the backup copy, roll back | v1 active; 0.024 s |
| Event history | 1:PROMOTE, 2:PROMOTE, 3:ROLLBACK, 4:PROMOTE, 5:ROLLBACK |

The PASS decisions in steps 4 to 7 are fixtures. They exercise the switch and rollback
paths only. With the committed configuration, no real decision can be `PASS`, so nothing
can be promoted. The times are for tiny files on one development machine.

Tests: `tests/test_model_registry.py` (in memory) and
`tests/integration/test_registry_persistence.py` (PostgreSQL: append-only tables, one use
per decision, a concurrent switch from the same champion gives one winner and one
`StaleChampion`, the CLI commands, migration downgrade and upgrade).

## Not covered

- Model rollback in a serving path: no model serves decisions yet, so no path loads the
  champion.
- The S3 object store: the drill used a local object store copy. The reconciliation code
  uses the same `ImmutableObjectStore` interface as the S3 store.
- Staging or production volumes, and restore under load.
