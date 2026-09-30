# OPS-02 evidence — backup, restore and reconciliation drill

Date: 2026-09-30. Status: **PASS** against the agreed recovery objectives.

Recovery objectives (user decision 2026-09-30, `configs/operations/recovery-objectives.json`,
version `f15-recovery-objectives-v1`): RTO 4 hours (14400 s), RPO 15 minutes (900 s).

All data is synthetic. No production store was used.

## Point-in-time restore drill (F15.7)

Scope: a disposable Compose stack (project `f15bk-smoke`, alternate host ports) built from
this commit, with the real backup services: WAL archiving (`archive_timeout` 60 s), the
`postgres-backup` base backup, and the scheduler tasks `backup_objects` and
`backup_journal` (every 5 minutes). PostgreSQL 17.11, SeaweedFS S3 gateway.

```sh
uv run python scripts/ops02_pitr_drill.py --project f15bk-smoke   --database-url postgresql+psycopg://tennis:local-only@127.0.0.1:55450/tennis   --object-endpoint 127.0.0.1:59020 --restore-port 55451
```

| Step | Result |
|---|---|
| Base backup at stack start (`postgres-backup`) | complete; `pg_basebackup` reported "all required WAL segments have been archived" |
| Seed (`tests/recovery_support.py::seed`) | 1 raw content, 8 decisions, 1 job run, 1 ledger with 2 virtual bets |
| Load: 1 database mark per second, 1 raw-object mark per 10 s, for 420 s | 42 object marks |
| Stop the API and the scheduler, fingerprint the `tennis` schema | 54 tables, revision `0013_model_registry` |
| More database marks for 90 s, then SIGKILL of PostgreSQL | 506 database marks written in total |
| New container, empty volume, `restore-pitr.sh` | primary after 2.517 s |
| Objects: backup copy into a new bucket | 13 objects restored |
| Compare every table (row count and ordered-row MD5) | no difference |
| Raw objects against the restored lineage | 1 of 1 verified |
| Ledger reconciliation | 1 ledger balanced, closing 80.00 PLN |
| Journal: live journal against the newest snapshot | equal chain (0 records: the stack journal was empty) |
| Restore and verification duration | **3.099 s** (RTO 14400 s) |

Data-loss window of each store (last acknowledged write to newest recovered write):

| Store | Recovered | Window | Expected worst case |
|---|---|---|---|
| PostgreSQL | mark 473 of 506 | 33.371 s | 60 s (`archive_timeout`) |
| Raw objects | 12 of 42 marks | 303.112 s | 300 s (object backup interval) plus the task run time |
| F01 journal | equal chain | 0.0 s | 300 s |

Report: `integrity: PASS`, `status: PASS`, no finding. Measured data loss **303.112 s**
(RPO 900 s), the largest window of the three stores.

Interpretation:

- The WAL archive restored every change up to the last archived segment. The SIGKILL lost
  the unarchived tail (33 marks), as designed. The loss stays below `archive_timeout`.
- The object window is about one backup interval. Every object older than the last
  backup was restored. A 5-minute interval keeps the worst case below the RPO.
- The journal check is weak evidence here: the drill journal had no records. The unit
  tests (`tests/test_operations_backups.py`) cover snapshots of a journal with records.
- The times are for a small synthetic data set on one development machine. They are not
  an estimate of production restore time. A larger database needs a new drill.

## Logical restore drill (`pg_dump`)

Scope: a disposable PostgreSQL 17.6 test container, a temporary F01 journal and a
temporary local object store.

```sh
TEST_DATABASE_URL=postgresql+psycopg://tennis:test-only@127.0.0.1:55444/tennis_test   MSYS_NO_PATHCONV=1 uv run python scripts/ops02_restore_drill.py --container f15i-test-pg
```

The drill seeds the same synthetic bundle, backs up with `pg_dump -Fc`, the SQLite online
backup and a directory copy, restores into a new database with `pg_restore`, then compares
every table, the Alembic revision (`0013_model_registry`), both journals (2 records), raw
objects (1 of 1 verified) and the ledger (balanced, closing 80.00 PLN).

Report (rerun 2026-09-30 with the agreed objectives): `integrity: PASS`, `status: PASS`,
no finding. Restore and verification 1.126 s. Data-loss window 0.0 s, because nothing
wrote between the backup and the failure point. A `pg_dump` copy alone does not meet the
RPO; the WAL archive does.

## Negative controls

`tests/integration/test_recovery_persistence.py`, `tests/test_operations_recovery.py` and
`tests/test_operations_backups.py`:

| Change | Detected as |
|---|---|
| One added decision row | `TABLE:decision_record` |
| A deleted raw object | `OBJECT_MISSING:<content_id>` |
| A changed journal payload in the backup | `JOURNAL_HASH_MISMATCH:<revision>` and `JOURNAL:MISMATCH` |
| Restore slower than the RTO | `OBJECTIVES_NOT_MET` |
| No measured restore time or data loss | `OBJECTIVES_NOT_MEASURED` (`FAIL`) |
| No objectives | `OBJECTIVES_UNSET` (`BLOCKED`) |
| An object key with other bytes in the backup | `OBJECT_CONFLICT`; the old recovery point stays |
| A schedule interval above the RPO | `SCHEDULE_EXCEEDS_RPO:<store>` |

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
- A copy off the host. The backup volumes are on the same host, so a host loss is
  outside the objectives.
- Restore with a `RECOVERY_TARGET_TIME`, and a base backup older than the newest one.
- Staging or production volumes, and restore under load.
