# F15.8 incident runbooks

These runbooks use only commands that exist in this repository. Every runbook follows the
same order:

1. **Stop** the affected path first. Do not wait for the root cause.
2. **Preserve** evidence. Open an incident bundle before any repair.
3. **Identify** the affected recommendation IDs, sources and versions.
4. **Repair** under new versions. Never edit or delete history.
5. **Replay** and evaluate the affected scope.
6. **Resume** only through a reviewed operation.
7. **Record** the result in the incident notes.

General rules:

- Corrections create new versions: a new F12 decision version with `supersedes`, a new
  policy version, a new parser version or a ledger adjustment. Append-only tables reject
  `UPDATE` and `DELETE`.
- An operator can stop. Only a policy reviewer can resume a source or turn the global
  stop off. Resuming never overrides a policy, a kill switch or an expired review.
- Keep incident bundles in access-controlled storage. They can hold licensed values. Never
  commit a bundle, a dump or a log to Git.
- Put times in UTC with an offset, for example `2026-09-20T12:00:00+00:00`.

## Common commands

Stop one source or everything:

```powershell
uv run tennis-governance source-stop <source_id> on --reason "<incident reason>"
uv run tennis-governance global-disable on --reason "<incident reason>"
```

Open an incident. This applies the stops, then writes the bundle:

```powershell
uv run tennis-ops open-incident --category PARSER_DRIFT --summary "<what happened>" `
  --window-start <start> --window-end <end> --source <source_id> --stop-sources
uv run tennis-ops verify-incident var/artifacts/incidents/<incident_id>
```

Resume after review (policy reviewer only):

```powershell
uv run tennis-governance source-stop <source_id> off --reason "<root cause and evidence>"
uv run tennis-governance global-disable off --reason "<root cause and evidence>"
```

## Source failure

Signals: `source_observation_age_seconds` above the limit, `TELEMETRY_MISSING`, F14 source
status `STALE` or `UNKNOWN`, fetch failures in F03.

1. F14 read checks already serve stale quotes as `NO_BET`. Confirm with
   `GET /v1/tennis/source-health`.
2. If the failure is an access-control failure or a rate-limit response, stop the source
   (`source-stop <id> on`). Respect `Retry-After`. Never bypass access controls.
3. Open a `SOURCE_FAILURE` incident for the outage window.
4. When the source returns, poll again. A failed poll never refreshes freshness.
5. A reviewer resumes the source after the fetch succeeds and the quote history is fresh.

## Parser drift

Signals: `parser-zero-events`, `parser-invalid-records`, `parser-duplicate-selections`
(critical, automatic source stop), `parser-unknown-labels`, `parser-volume-change`.

1. Critical rules stop the source automatically (`tennis-ops evaluate-alerts --apply`).
2. Open a `PARSER_DRIFT` incident with the source and the drift window.
3. Keep the raw responses. F03 dead letters hold the malformed inputs.
4. Write a new parser version with golden fixtures for the new shape.
5. Replay the archived raw responses with the new parser version (F03 replay). The old
   parse results stay.
6. A reviewer resumes the source after the replay matches the fixtures.

## Identity error

Signals: a wrong player mapping, an identity review queue above its limit.

1. Stop the affected sources, or turn the global stop on when the scope is unclear.
2. Open an `IDENTITY_ERROR` incident. The bundle lists every decision in the window.
3. Correct the identity through F04 manual review. The correction is a new alias or merge
   version with history.
4. Rebuild the affected features and decisions as new versions. Old decisions stay in
   audit, superseded.
5. A reviewer resumes after the replay shows the corrected mapping.

## Bad settlement

Signals: `settlement-mismatch` (critical, automatic global stop), an unbalanced ledger.

1. The global stop is on. Confirm with `tennis-governance export`.
2. Open a `BAD_SETTLEMENT` incident for the settlement window.
3. Run the ledger reconciliation (`reconcile_ledgers`) and keep its output.
4. Correct with a new settlement record and a ledger adjustment
   (`apply_settlement(..., correction_reason=...)`). Never edit an entry.
5. Check the rule version that was effective at bet time. Unknown outcomes stay pending.
6. A reviewer turns the global stop off after the ledger balances.

## Future leakage

Signals: `future-leakage` (critical), a leakage test failure in CI.

1. Turn the global stop on. Leakage makes every model-based decision suspect.
2. Open a `FUTURE_LEAKAGE` incident for the window since the affected feature version.
3. Find the feature or dataset version that used unavailable data.
4. Build a new feature version. Old snapshots stay unchanged.
5. Rerun the F13 walk-forward evaluation and the leakage suite on the new version.
6. A reviewer resumes only after the promotion gates pass again.

## Compromised artifact

Signals: a hash mismatch on a model artifact, a manifest or an incident bundle.

1. Turn the global stop on.
2. Open a `COMPROMISED_ARTIFACT` incident.
3. Do not load the artifact. `load_booster` refuses bytes that do not match their hash.
4. Rebuild the artifact from its recorded inputs, or restore it from a verified backup.
   When the artifact belongs to the champion model, roll back to a complete verified
   bundle: `uv run tennis-ops registry rollback --family <family> --reason "<incident>"`.
   A rollback target with a changed file is refused; restore the file first, then check
   it with `tennis-ops registry verify --bundle <bundle-id>`.
5. Rotate every credential that the attacker could read (`create-api-token --rotate`,
   database and object-store passwords).
6. A reviewer resumes after the hashes and the evaluation match.

## Risk-store outage

Signals: HTTP 503 `DEPENDENCY_UNAVAILABLE`, `tennis_dependency_errors_total`, readiness
503, reservation failures in F12.

1. F12 cannot reserve exposure, so it cannot publish a `BET`. F14 returns 503 when the
   decision store fails. No stale `BET` is served.
2. Open a `RISK_STORE_OUTAGE` incident when the outage is longer than a short blip.
3. Restore the database if needed (see "Database loss" below). Verify it with
   `tennis-ops verify-restore`.
4. Reconcile reservations and the ledger before new decisions.

## Stale publication

Signals: `stale-publication` (critical, automatic global stop).

1. The global stop is on.
2. Open a `STALE_PUBLICATION` incident. The bundle lists the affected decisions.
3. Mark each affected decision with a superseding `NO_BET` version.
4. Find why the F12 publication recheck or the F14 read check did not block it. Add a
   regression test.
5. A reviewer turns the global stop off after the fix is deployed.

## Agent incident

Signals: `tennis_agent_critical_attempts_total` increases, a verifier finding such as
`FABRICATED_EVIDENCE` or `HARD_GATE_OVERRIDE`, or a report of a wrong agent text.
Deterministic decisions do not depend on an agent, so this stop does not stop
recommendations.

1. Stop the role. The stop key is `agent:<prefix>`, for example `agent:ag-ex`:
   `uv run tennis-governance source-stop agent:ag-ex on --reason "<incident reason>"`.
   The next tool call and the next run of that role return `DISABLED`. F14 then serves
   the deterministic explanation.
2. Preserve the traces. `tennis.agent_trace` and `tennis.agent_proposal` are append-only.
   A trace holds codes, IDs and hashes, not evidence text.
3. Identify the affected traces, proposals and recommendation IDs from the trace IDs.
4. Add the case to the development fixtures (not to a sealed release set). Change the
   prompt, tools or verifier under a new role version.
5. Run the agent evaluation again. Keep the role off until it passes.
6. A reviewer resumes the role: `source-stop agent:ag-ex off --reason "<review>"`.

## Backup failure

Signals: `TennisBackupStale`, `TennisBaseBackupStale`, `TennisBackupMissing` or
`TennisWalArchiveFailing`. The recovery objectives are RTO 4 hours and RPO 15 minutes
(`configs/operations/recovery-objectives.json`, user decision 2026-09-30).

1. A backup failure does not stop publication by itself. The RPO is at risk until the
   backup runs again. Treat it as urgent.
2. Find the failed store in the alert label `store`, or in the logs:
   `docker compose logs postgres-backup scheduler postgres`.
3. WAL archive: check `pg_stat_archiver` (`failed_count`, `last_failed_wal`) and the free
   space of the `postgres-backup` volume. A segment name with other bytes in the archive
   is a conflict. Do not delete or overwrite an archived segment; preserve both and open
   an incident. PostgreSQL keeps each segment until it is archived, so a full data disk
   is the next risk.
4. Base backup: run one backup now, then check that a new `COMPLETE` marker exists:
   `docker compose exec postgres-backup sh /etc/tennis-postgres/base-backup.sh --once`.
5. Objects or journal: check the scheduler task outcome in the logs
   (`backup_objects`, `backup_journal`). An `OBJECT_CONFLICT` means a key with other
   bytes. Preserve both copies and open an incident.
6. The alert clears when a new recovery point exists. Record the gap in the incident notes.

## Database loss (point-in-time restore)

Use this when the PostgreSQL data volume is lost or damaged. Do not start the damaged
server again. Keep the damaged volume for evidence until the restore is verified.

1. Turn the global stop on. Stop the API and the scheduler:
   `docker compose stop api scheduler`.
2. Record the start time. The RTO is 4 hours from here.
3. Start a PostgreSQL container with an empty data volume, the `postgres-backup` volume at
   `/backup` (read-only) and `deploy/postgres` at `/etc/tennis-postgres`. Use the
   entrypoint `sh /etc/tennis-postgres/restore-pitr.sh`. It extracts the newest complete
   base backup, replays the WAL archive and promotes the server. Set
   `RECOVERY_TARGET_TIME` to stop before a known bad write. Set `BASE_BACKUP` to use an
   older base backup.
4. Wait until `SELECT pg_is_in_recovery()` returns `false`.
5. Restore raw objects: copy the `app-backup` volume directory `objects` into the bucket.
   Existing keys are kept. Restore the journal from the newest snapshot in `journal`.
6. Verify: `tennis-ops verify-restore --expected <fingerprint> --journal-source <journal>
   --journal-restored <snapshot> --measured-restore-seconds <s>
   --measured-data-loss-seconds <s>`. Use the newest fingerprint that you have. Without
   a fingerprint, check `reconcile_raw_objects` and the ledger balance.
7. Point the Compose `postgres` service at the restored volume. Start the services.
8. Reconcile reservations and the ledger before new decisions. A policy reviewer turns the
   global stop off only after the report is `PASS`.
9. Take a new base backup at once
   (`docker compose exec postgres-backup sh /etc/tennis-postgres/base-backup.sh --once`).
   The restored server starts a new timeline.

The drill `scripts/ops02_pitr_drill.py` runs these steps on a disposable stack.
Results: [OPS-02](evidence/OPS-02.md).
