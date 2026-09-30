# F15 operations and security guide

F15 makes the service observable and recoverable. It stops publication when its inputs or
controls cannot be trusted. Deterministic code owns every stop. No code path places a bet.

## Status

Demonstrated on synthetic fixtures and isolated local services:

- F15.3 metrics: an in-process registry with the Prometheus text format at `GET /metrics`.
- F15.3 signals: freshness, parser drift, missing-value rate, probability drift (PSI) and
  ledger balance.
- F15.4 versioned alert rules (`configs/operations/alert-rules.json`, version
  `f15-alert-rules-v1`, status `ACCEPTED` by the owner on 2026-09-30 for shadow operation).
- F15.3/F15.4 monitoring stack in Compose: Prometheus, Alertmanager and a read-only Grafana
  dashboard. The Prometheus rules are generated from the rule set. See "Monitoring stack".
- F15.4 deterministic controls: a critical alert stops the affected source, or turns the
  global stop on, through the F01 journal.
- F15.1 job graph for the blueprint section 33.1 jobs, dependency and publication gates,
  separate backfill capacity, and idempotent job runs.
- F15.1 scheduler (`tennis-ops scheduler`, ADR 0005): the job graph and the alert
  evaluation with deterministic controls on a cadence.
- F15.2 resource leases with expiry and fencing tokens (migration `0011_operations`).
- F15.5 security: read-only API mounts and root file system in Compose, one
  least-privilege PostgreSQL role per Compose component with a start-up privilege check,
  token digests only, placeholder refusal in production, and a source scan for unsafe
  deserialization.
- F15.7 restore verification: database and journal fingerprints, raw-object and ledger
  reconciliation, and incident bundles with affected recommendation IDs.
- F15.7 backups for the agreed recovery objectives (RTO 4 hours, RPO 15 minutes; user
  decision 2026-09-30): continuous WAL archiving, daily base backups, object and journal
  backups every 5 minutes, retention, backup metrics and alerts, and a point-in-time
  restore drill (`scripts/ops02_pitr_drill.py`). See "Backup and restore (F15.7)".
- F15.7 model rollback: the F11.8 registry and F13.9 audited champion switch, and a
  rollback drill (`scripts/ops02_rollback_drill.py`).
- F15.8 runbooks: [runbooks.md](runbooks.md).
- F15.6 agent tool scoping: a server-side tool gateway, an agent kill switch on the F01
  stops, append-only agent traces and proposals (migration `0012_agent_records`) and
  agent metrics. See "Agent tool scoping (F15.6)" and [the agent guide](../agents/README.md).

Evidence: [OPS-01](evidence/OPS-01.md) and [OPS-02](evidence/OPS-02.md).

Pending or not implemented: see "Not implemented" at the end of this file.

## Metrics (F15.3)

`GET /metrics` returns the Prometheus text format (version 0.0.4). It needs a token with
the `operator` or `policy_reviewer` role. Viewer and agent roles get 403. Without F14
serving, the route returns 503. The response is not cacheable.

| Metric | Type | Labels |
|---|---|---|
| `tennis_http_requests_total` | counter | `route` (template), `method`, `status` |
| `tennis_http_request_duration_seconds` | histogram | `route` |
| `tennis_rechecked_records_total` | counter | `recorded`, `served` |
| `tennis_read_time_blocks_total` | counter | `reason` (code without IDs) |
| `tennis_dependency_errors_total` | counter | `error` (exception type) |
| `tennis_source_status` | gauge | `source_id`, `status` |
| `tennis_source_observation_age_seconds` | gauge | `source_id` |
| `tennis_recommendations_allowed` | gauge | `account_scope` |
| `tennis_metrics_collector_up` | gauge | `collector` |
| `tennis_agent_runs_total` | counter | `role`, `status` |
| `tennis_agent_tool_calls_total` | counter | `role`, `tool` (catalog name or `unknown`), `outcome` |
| `tennis_agent_critical_attempts_total` | counter | `role`, `reason` |
| `tennis_agent_tokens_total` | counter | `role`, `direction` |
| `tennis_agent_fallbacks_total` | counter | `role` |

`AgentMetrics.record(trace)` adds the agent samples. A tool name outside the catalog and
the forbidden list becomes `unknown`, so a model cannot put free text in a label.

Rules for labels:

- A route label is the template, for example `/v1/audit/recommendations/{recommendation_id}`.
  It never holds a record ID.
- Label values are cut to 96 characters. Characters other than letters, digits and
  `_ . : / { } -` become `_`. A label cannot hold a token, a URL query or free text.
- A source without a quote observation has no age sample. Its status is `UNKNOWN`. An
  absent sample is not healthy.
- A collector that fails reports `tennis_metrics_collector_up 0`, and its samples are
  absent.

Counters are per process. A restart sets them to zero. Prometheus handles counter resets.


## Monitoring stack (F15.3, F15.4)

Compose runs three pinned monitoring images. Every port binds to `127.0.0.1` only.

| Service | Image (tag and digest in `compose.yaml`) | Port | Purpose |
|---|---|---|---|
| `prometheus` | `prom/prometheus:v3.15.0` | 9090 | Scrapes the API, the scheduler, itself and Alertmanager every 30 s. Keeps 30 days. |
| `alertmanager` | `prom/alertmanager:v0.34.1` | 9093 | Routes alerts by severity. |
| `grafana` | `grafana/grafana:13.0.10-distroless-slim` | 3000 | Read-only dashboard "Tennis operations". |
| `monitoring-token` | application image | none | Stores the digest of the Prometheus scrape token before the API starts. |

Scraping:

- `tennis-api` scrapes `GET /metrics` with a bearer token from the Compose secret
  `prometheus_api_token`. The token has the `operator` role. `register-api-token` stores
  only its digest. The plain token stays in the secret file.
- `tennis-scheduler` scrapes the scheduler on the internal port 9101.

Rules: `deploy/prometheus/rules/tennis-alerts.yml` is generated. Do not edit it.

```powershell
uv run tennis-ops prometheus-rules            # regenerate from the rule set
uv run tennis-ops prometheus-rules --check    # exit 1 when the file is out of date
uv run tennis-ops grafana-dashboard --check
```

| Alert group | Content |
|---|---|
| `tennis-signal-rules` | Two alerts per rule: `<Name>` when the value breaches the threshold, and `<Name>TelemetryMissing` when an expected signal has no value or is older than 900 s. Labels carry `severity`, `rule_id`, `rule_set`, `reason`, `control` and `scope`. |
| `tennis-telemetry` (`f15-telemetry-rules-v1`) | A scrape target down or absent, a stale scheduler tick or alert evaluation, no expected signal, a failed collector or signal producer, and failed jobs or tasks. |

Missing telemetry always alerts. A stopped exporter makes `up == 0`, then its series
disappear, and the `absent` and staleness alerts fire. A test checks that each rule and
dashboard query names an exported metric. A test checks that the committed files equal the
generator output. CI runs `promtool check config`, `promtool test rules` on
`deploy/prometheus/tests/tennis-alerts.test.yml`, and `amtool check-config`.

The scheduler applies the stop. Prometheus does not. So an alert route failure cannot
delay a stop.

Routing (`deploy/alertmanager/alertmanager.yml`): `severity="critical"` goes to
`operator-critical` (repeat every hour); everything else goes to `operator-warning` (repeat
every 4 hours). Both receivers post to the scheduler webhook. The webhook logs the alert
name, severity, rule, scope and status, and counts `tennis_alert_notifications_total`. It
logs no annotation and no value. A down scheduler inhibits the rule-set missing-telemetry
alerts, so the cause is reported once. An external channel (e-mail or chat) needs an
approved outbound host first; it is not configured.

Grafana: anonymous visitors are viewers. Sign-up, analytics, update checks, the news feed
and plugin installs are off. The data source and the dashboard are provisioned from files
and cannot be changed or deleted in the UI. The admin password comes from the Compose
secret `grafana_admin_password`.

Local secrets: `uv run python scripts/init_local_secrets.py` writes random files into
`./secrets` (ignored by Git and Docker). Set `TENNIS_SECRETS_DIR` for another directory.
Production uses the host's secret manager. The directory is private (0700). Each file is
readable by the container user (0644), because a Compose file secret is a bind mount.

Upstream images: CI scans the five third-party Compose images with Trivy as a report
only, because a fix needs an upstream release. The application image keeps the blocking
scan. See [OPS-01](evidence/OPS-01.md) for the findings.

## Signals and alert rules (F15.3, F15.4)

A signal is one value for one scope at one time (`tennis_engine.monitoring.signals.Signal`).
A value of `null` means that the measurement is missing.

| Signal | Scope | Producer |
|---|---|---|
| `source_observation_age_seconds` | source | `freshness_signals` from F14 source health |
| `parser_events`, `parser_valid_rate`, `parser_unknown_label_rate`, `parser_duplicate_selection_ids`, `parser_volume_change` | source | `parser_signals` from F05 `SnapshotMetrics` |
| `feature_missing_rate` | global | `missing_rate` |
| `model_probability_psi` | global | `population_stability_index` |
| `settlement_mismatch_count` | global | `ledger_signals` from F06 ledger reconciliation |
| `stale_publication_count`, `leakage_test_failures`, `payout_inconsistency_count`, `identity_review_queue_size` | global | Supplied by the job that measures them |

The rule set is versioned. Each rule has a signal, a comparison, a threshold, a severity
and an optional control. Evaluation rules:

- A breached threshold raises an alert with reason `THRESHOLD`.
- A missing value, a missing signal for an expected source, or a signal older than
  `max_signal_age_seconds` raises an alert with reason `TELEMETRY_MISSING`.
- `missing_applies_control: false` keeps the alert but does not apply the control for
  missing telemetry. The proposed rules use it for global signals that come from separate
  runs, for example the leakage suite.
- Only a `CRITICAL` rule can name a control. `SOURCE_STOP` needs a source-scoped rule.

User decision, 2026-09-30: the owner accepted the proposed thresholds unchanged as version
1 (`f15-alert-rules-v1`, status `ACCEPTED`) for shadow operation. Blueprint section 34.5 and
the F05 parser-drift proposal are their basis. They are not tuned on real data. Retune them
with a new version after real data arrives. Change a threshold only with a new version and
a reason. A test pins the accepted values.

## Deterministic controls (F15.4)

`apply_controls(alerts, store)` uses the F01 journal with an operator principal:

| Control | Effect |
|---|---|
| `SOURCE_STOP` | `set_source_stop(source, True)`. `can_fetch` denies it with `SOURCE_STOPPED`. F14 serves every record from it as `NO_BET`. |
| `GLOBAL_STOP` | The global stop turns on. Every responsible-use lookup is denied. |

A control only stops. It never resumes. Applying the same alerts again appends nothing.
Only a reviewer can resume a source (`tennis-governance source-stop <id> off`) or turn the
global stop off. F14 reads the journal on every current read, so a stop takes effect on
the next request. It does not wait for an operator to see the alert.

Any rule set status can apply controls. A stop is the safe direction, so this needs no
approval.

```powershell
uv run tennis-ops evaluate-alerts --signals var/signals.json --sources betclic-odds,superbet-odds
uv run tennis-ops evaluate-alerts --signals var/signals.json --sources betclic-odds --apply `
  --database var/governance.sqlite3 --access-file var/governance-access.json
```

Exit codes: 0 no alert, 1 at least one alert, 2 an input or storage error. An invalid
signal file gives only the field locations, not the values.

## Jobs and dependencies (F15.1)

`tennis_engine.operations.jobs` holds the 20 jobs of blueprint section 33.1 and their
dependencies (section 33.2). The graph has no cycle. A test checks this.

- A job runs only when every dependency is `COMPLETE`. A missing status is `INCOMPLETE`.
- `publish_recommendations` also needs the `identity`, `format`, `policy` and `quote`
  inputs to be `COMPLETE`. Otherwise the attempt is `BLOCKED` with the reasons.
- Backfills and replays use the `BACKFILL` capacity pool. Prospective jobs use the
  `PROSPECTIVE` pool. A full backfill pool cannot take a prospective slot. The proposed
  limits are 4 and 1 per process.

A job run is identified by a SHA-256 key of the job, resource, cutoff and input versions.
The scheduler is not trusted for exactly-once delivery:

| Event | Behaviour |
|---|---|
| Duplicate delivery or scheduler restart | The same key finds the run. A run that succeeded returns `ALREADY_SUCCEEDED` and does nothing. |
| Worker crash (exception) | A `FAILED` attempt is appended and the lease is released. A retry can succeed. |
| Too many failures | After `max_attempts` failures the run is `EXHAUSTED`. |
| Stale lock | See leases. The stale worker's success is refused. |

`tennis.job_run` and `tennis.job_attempt` are append-only. A partial unique index allows
one `SUCCEEDED` attempt per run. A `RUNNING` or `SUCCEEDED` attempt must carry a fencing
token.

## Scheduler (F15.1)

`tennis-ops scheduler` runs the job graph and the operations tasks on a cadence. It is a
small loop in `tennis_engine.operations.scheduler`. [ADR 0005](../adr/0005-f15-scheduler.md)
records why it replaces Prefect (Prefect added 78 packages and 103 MB, and a second run
state). The Compose service `scheduler` runs it.

| Rule | Behaviour |
|---|---|
| Tick | Every 60 s. One tick walks the graph in topological order. |
| Job cutoff | The start of the job's window (`every`). A job runs once per window. |
| Duplicate tick, restart, second scheduler | The same run key gives `ALREADY_SUCCEEDED`. The effect runs once. |
| Job without a handler | `NOT_CONFIGURED`, so it counts as `INCOMPLETE`. Its dependants are `BLOCKED`. |
| Store failure | The job is `FAILED` for this tick. Its dependants are `BLOCKED`. The loop continues. |
| Operations task | Runs once per window under the lease `task:<name>`. A failure is retried on the next tick. |
| Stop | `SIGTERM` ends the loop after the current tick. |

Only `sync_source_registry` has a handler. It reads the F01 register at the cutoff and
records the journal revision and the number of fetchable sources. It changes nothing. So
every job that needs collected data is `NOT_CONFIGURED`, and `publish_recommendations`
never runs. A new job needs a handler and a test.

The operations task `evaluate_alerts` runs every minute:

1. Collect signals: source freshness (F14 source health), ledger balance
   (`reconcile_ledgers`) and the signal inbox (`var/signals/*.json`).
2. Evaluate the versioned rule set. The expected sources are the sources that the F01
   register allows to be fetched now.
3. Apply the deterministic controls to the F01 journal (operator principal).
4. Keep the result as one snapshot for the next scrape.

Other jobs report a signal through the inbox with `monitoring.cadence.write_signals`. An
invalid file is skipped and counted in `tennis_signal_inbox_invalid_files`.

```powershell
uv run tennis-ops scheduler --once          # one tick, print the report
uv run tennis-ops scheduler --no-apply      # evaluate alerts but apply no control
```

The scheduler serves on port 9101 inside the container: `GET /metrics`, `GET /health`
(503 before the first tick or when the last tick is older than three ticks) and
`POST /alertmanager`. The access log is off. The webhook logs alert names, severities and
scopes only.

| Metric | Type | Labels |
|---|---|---|
| `tennis_scheduler_ticks_total` | counter | |
| `tennis_scheduler_last_tick_timestamp_seconds` | gauge | |
| `tennis_job_outcomes_total` | counter | `job`, `state` |
| `tennis_job_last_success_timestamp_seconds` | gauge | `job` |
| `tennis_job_configured` | gauge | `job` |
| `tennis_task_runs_total` | counter | `task`, `state` |
| `tennis_task_last_success_timestamp_seconds` | gauge | `task` |
| `tennis_signal_value` | gauge | `signal`, `scope` (absent when missing) |
| `tennis_signal_observed_timestamp_seconds` | gauge | `signal`, `scope` |
| `tennis_signal_expected` | gauge | `signal`, `scope` |
| `tennis_signal_producer_up` | gauge | `producer` |
| `tennis_deterministic_alert` | gauge | `rule_id`, `severity`, `reason`, `scope` |
| `tennis_alert_evaluation_timestamp_seconds` | gauge | |
| `tennis_controls_applied_total` | counter | `control` |
| `tennis_alert_notifications_total` | counter | `alertname`, `severity`, `status` |

## Leases and fencing (F15.2)

A lease names one resource, for example `source:<source_id>:<resource>` or
`job:<job>:<scope>` (blueprint section 33.3). Rules:

- A lease lasts more than 0 seconds and at most one hour.
- A new holder gets a larger fencing token. A renewal keeps the token. A trigger rejects a
  token decrease.
- An effect is valid only while its token is current and the lease has not expired. The
  PostgreSQL store locks the lease row and writes the effect in one transaction.
- A worker that loses its lease cannot record success. Its attempt becomes `FAILED` with
  `LEASE_LOST`.

A job's own effects must also be idempotent or fenced. For example, decision records use
`ON CONFLICT DO NOTHING` on their ID. The runner fences the success record, not every
write inside the work function.

## Security (F15.5)

### Least-privilege matrix

| Principal | Recommendations, analysis | Audit | Metrics | Governance journal | Database |
|---|---|---|---|---|---|
| `dashboard` token | read | no | no | none | none |
| `agent` token | read | no | no | none | none |
| `operator` token | read | read | read | none through the API | none |
| `policy_reviewer` token | read | read | read | none through the API | none |
| API process | serves the above | | | read-only SQLite connection, read-only mount | `tennis_api`: `SELECT` only |
| Operator (CLI) | | | | stop a source, global stop on, archive documents | migrations, backups |
| Policy reviewer (CLI) | | | | all operator actions, approve policies, resume | |

No F14 or F15 HTTP route writes. No role can change identities, policy, funds, models or
risk through the API. Tests check each denial.

### Secrets

- Settings wrap secrets in `SecretStr`. `show-config` and the start-up log show no secret
  and no file path.
- Production refuses a missing or placeholder database password, placeholder object-store
  keys, object storage without TLS, and a credential file that is missing, empty or holds
  a placeholder token.
- The credential file holds SHA-256 digests only. `create-api-token` prints the token once.
- Metric labels cannot hold a token (see "Metrics"). The JSON logger redacts keys that look
  like secrets.
- Store production secrets in the host's secret manager or Compose secrets. Never commit
  `.env`.

### Database roles per component

User decision 2026-09-30 (user-accepted): the production target is one private host
that runs the Compose stack. Each component has its own PostgreSQL role. Docker network
egress allow lists and encrypted volumes with SeaweedFS encryption are later slices.

| Role | Used by | Privileges |
|---|---|---|
| `tennis` (owner) | `migrate`, `db-roles`, `admin` (one-shot services only) | Owns the schema. It is the image's bootstrap superuser. |
| `tennis_api` | `api`; also `object-store-init` and `monitoring-token`, which do not connect | `SELECT` on every `tennis` table and on `alembic_version`. No write. |
| `tennis_scheduler` | `scheduler` | `SELECT` on every `tennis` table and on `alembic_version`. `INSERT` on `job_run` and `job_attempt`. `INSERT`, `UPDATE` on `resource_lease`. No `DELETE`. |
| `tennis_agent` | F18 agent store (`build_agent_store`); no Compose service yet | `SELECT`, `INSERT` on `agent_trace` and `agent_proposal` only. No other table, no `UPDATE`, `DELETE` or `TRUNCATE`. |
| `tennis_backup` | `postgres-backup` | `REPLICATION` only. No table privilege. |

No role except the owner has `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `BYPASSRLS`, a role
membership or `CREATE` on a schema or on the database. Prometheus and Grafana do not
connect to PostgreSQL, so there is no monitoring role. Compose has no worker or
ingestion service yet. Ingestion commands run in `admin` with the owner role.

How it works:

- `db-roles` runs `tennis-ops provision-roles --secrets-dir /run/secrets` after each
  `migrate`. It creates a missing role, sets each password from its secret file, revokes
  every table privilege and grants only the privileges in the table above
  (`src/tennis_engine/operations/roles.py`).
- It then reads the effective privileges back (`has_table_privilege`, role attributes and
  memberships). A role with more or less privilege stops `db-roles` with an error. Then
  `api`, `scheduler` and `postgres-backup` do not start (fail closed).
- Each service gets its role in `TENNIS_DATABASE_URL` without a password. The password
  comes from `TENNIS_DATABASE_PASSWORD_FILE` (a Compose secret). The agent store uses
  `TENNIS_AGENT_DATABASE_URL` and `TENNIS_AGENT_DATABASE_PASSWORD_FILE`. It has no
  fallback to the main role. `TENNIS_SERVING_DATABASE_PASSWORD_FILE` does the same for
  `TENNIS_SERVING_DATABASE_URL`.
- Secrets: `postgres_api_password`, `postgres_scheduler_password`,
  `postgres_agent_password` and `postgres_backup_password`. For local use,
  `scripts/init_local_secrets.py` writes them. Each reader strips the line end (LF or
  CRLF). A password shorter than 16 characters or a placeholder is refused.
- Run `db-roles` again after a manual migration:
  `docker compose run --rm db-roles`.
- `tennis-ops grant-reader --role <name>` still gives an existing role the API
  privileges, for a deployment outside Compose.

Integration tests on an isolated PostgreSQL (`tests/integration/test_component_roles.py`)
check that each role does its work and gets `permission denied` outside its scope. The
agent role cannot read or write the ledger, decisions, the model registry or leases, and
cannot update, delete or truncate its own tables. A Compose smoke test on 2026-09-30 showed
each service healthy under its own role, migrations applied, a base backup and WAL
archiving working with `tennis_backup`.

### Containers

- The API container has a read-only root file system and read-only volume mounts.
- The image runs as the non-root user `tennis` (UID 10001) and has no `pip`.
- CI scans the image with Trivy (CRITICAL and HIGH, fixed issues only) and runs
  `pip-audit`.

### Code and artifacts

- A test scans `src/` for `pickle`, `joblib`, `marshal`, `shelve`, `eval`, `exec`,
  `yaml.load`, `torch.load` and `shell=True`. There are none.
- Tabular models are JSON boosters. `load_booster` checks the SHA-256 before it loads.
- Incident bundles and journal backups have hash manifests. A changed file is detected.

Encryption at rest and in transit depends on the deployment (disk encryption, TLS on
PostgreSQL). Production requires TLS for object storage. Outbound network allow lists are
a deployment control; no rule is in the repository yet.

## Agent tool scoping (F15.6)

The F18 gateway (`tennis_engine.agents.gateway.ToolGateway`) runs on the server. It checks
each tool attempt in this order: tool budget, deadline, kill switch, tool name and role
allowlist, argument schema, authorized subject IDs and cited evidence.

| Rule | Behaviour |
|---|---|
| Tool catalog | Read tools and review-queue proposal tools only. No SQL, shell, network, bet, policy, merge, approval or re-enable tool exists. |
| Forbidden or unknown name | Denied. Recorded as a critical attempt, for example `FORBIDDEN_ACTION:place_bet`. |
| Tool of another role | Denied. Recorded as `TOOL_NOT_ALLOWED:<tool>`. |
| Subject outside the run scope | Denied. Recorded as `OUT_OF_SCOPE:<tool>`. |
| Proposal that cites evidence the run did not receive | Denied. Recorded as `UNSEEN_EVIDENCE:<tool>`. |
| Secret record | Never returned to an agent. |
| Record not available at the cutoff | Never returned to an agent. |
| Restricted record | Returned without values and text, marked `withheld`. |
| Evidence text | Delivered as data. The prompt says that it never gives instructions. Authority comes only from the role allowlist. |

A run with a critical attempt is `REJECTED`, even when its final output is valid. The
gateway protection and the agent behaviour are reported separately.

### Kill switch

The agent kill switch uses the existing F01 stops. It adds no new mechanism:

| Stop | Effect |
|---|---|
| Global stop on | Every agent role is `DISABLED`. An empty journal has the global stop on. |
| `source-stop agent:<prefix> on` | One role is `DISABLED`, for example `agent:ag-ex`. |
| Journal unreadable | The role is `DISABLED` with `SWITCH_UNAVAILABLE`. |

The runner checks the switch before the first model call. The gateway checks it before
each tool call, so a stop takes effect during a run. An operator can stop a role. Only a
policy reviewer can resume it. F18.8 role flags (`RoleFlags`) also keep every role off
until it is explicitly enabled.

### Audited traces

An `AgentTrace` holds codes, IDs, hashes and counts only: the tool label, the argument
hash, the subject, returned evidence IDs, the gateway outcome and the denial reason. It
holds no evidence text, no evidence value, no prompt and no model prose. A test checks
that a secret value does not appear in a trace or in the metrics.

`PostgresAgentStore` writes traces to `tennis.agent_trace` and proposals to
`tennis.agent_proposal`. Both tables are append-only (trigger). A proposal has state
`PROPOSED` only (check constraint). There is no apply column. A retry with the same
idempotency key stores nothing new, also under concurrent writers. A run whose trace
cannot be stored returns no output, so the caller falls back. Runbook: "Agent incident"
in [runbooks.md](runbooks.md).

## Backup and restore (F15.7)

### Recovery objectives

User decision, 2026-09-30: the owner accepted these recovery objectives for the single
private host (`configs/operations/recovery-objectives.json`, version
`f15-recovery-objectives-v1`, status `ACCEPTED`):

| Objective | Value | Meaning |
|---|---|---|
| RTO | 4 hours (14400 s) | The longest time from the start of a restore to a verified service. |
| RPO | 15 minutes (900 s) | The longest window of acknowledged writes that a restore can lose. |

The schedule in the same file is the implementation choice that meets the RPO. A test
checks that the worst-case loss of each store is at or below the RPO.

### Backups

| Store | Backup | Worst-case loss | Retention |
|---|---|---|---|
| PostgreSQL | Continuous WAL archiving (`archive_timeout` 60 s, `deploy/postgres/archive-wal.sh`) and a daily base backup (`postgres-backup` service, `pg_basebackup`) | 60 s | 7 base backups; every WAL segment since the oldest kept base backup |
| Raw objects | The scheduler task `backup_objects` copies each new key to the backup volume every 5 minutes. It never deletes. | 5 minutes | Every object (objects are immutable) |
| F01 journal | The scheduler task `backup_journal` takes a SQLite online backup every 5 minutes. | 5 minutes | 2016 snapshots (7 days) |

- The WAL archive command copies a segment to a temporary name, then renames it. The same
  segment with the same bytes is a success. The same name with other bytes is a failure,
  so PostgreSQL keeps the segment and retries.
- The backup role `tennis_backup` has `REPLICATION` only. Its password is the Compose
  secret `postgres_backup_password`. `pg_hba.conf` allows it only for replication.
- Backups are on the volumes `postgres-backup` and `app-backup`. Keep them on a separate,
  encrypted disk. There is no copy off the host, so a host loss is outside the objectives.

Metrics and alerts (all critical, generated rules `f15-telemetry-rules-v2`):

| Alert | Fires when |
|---|---|
| `TennisBackupStale` | The newest object or journal backup is older than the RPO (15 minutes). |
| `TennisBaseBackupStale` | The newest base backup is older than 26 hours. |
| `TennisBackupMissing` | A store has no recovery point for 30 minutes. |
| `TennisWalArchiveFailing` | `pg_stat_archiver` counts a failed WAL segment in the last 15 minutes. |

The scheduler reads the recovery points from the backup volumes at scrape time
(`tennis_backup_last_success_timestamp_seconds`, `tennis_wal_archive_*`). A restart does
not hide or invent a backup.

### Restore and verification

`deploy/postgres/restore-pitr.sh` restores into an empty data directory. It extracts the
newest complete base backup, replays the WAL archive and promotes the server. It refuses
a data directory that is not empty. `RECOVERY_TARGET_TIME` stops the replay earlier.

`verify-restore` compares row counts and ordered-row digests of every `tennis` table, the
Alembic revision and, when given, both journals. `reconcile_raw_objects` checks that each
archived content exists and matches its SHA-256. The ledger check confirms that every
virtual ledger balances.

A report compares the measured restore time and data-loss window with the agreed
objectives. It is `PASS` or `FAIL`. A missing measurement is `FAIL`
(`OBJECTIVES_NOT_MEASURED`). A call without objectives is still `BLOCKED`, so a missing
objective never passes. `verify-restore` reads the objectives from the configuration file
unless `--rto-seconds` or `--rpo-seconds` overrides them.

Drills:

- `scripts/ops02_pitr_drill.py`: the full point-in-time restore on a disposable Compose
  stack, after a SIGKILL of PostgreSQL. It measures the restore time and the data-loss
  window of each store.
- `scripts/ops02_restore_drill.py`: a logical `pg_dump` restore on a test container.

Results: [OPS-02](evidence/OPS-02.md). The steps: [runbooks.md](runbooks.md).

### Model rollback

The F11.8 registry (`tennis-ops registry`) holds complete model bundles. A champion switch
needs a `PASS` F13 decision and a reviewer who is not the author. A rollback restores one
complete, verified bundle or "no champion", and refuses a changed file. See
[the model guide](../models/README.md#f118-model-registry-and-f139-champion-switch-modelsregistrypy).
The drill `scripts/ops02_rollback_drill.py` passed; see [OPS-02](evidence/OPS-02.md). No
model serves decisions yet, so no serving path loads the champion.

## Incidents (F15.7)

`tennis-ops open-incident` applies the stops first, then writes an immutable bundle:

- `incident.json`: category, window, sources, affected recommendation IDs, stops applied.
- `decisions.jsonl`: the affected stored decisions, unchanged.
- `governance.json`: the journal export.
- `manifest.json`: the SHA-256 of each file.

An affected decision is any stored version decided in the window that uses a named source
or bookmaker. The command never changes a decision. `verify-incident` checks the hashes.
Runbooks: [runbooks.md](runbooks.md).

## Not implemented

- Handlers for the collection, feature, scoring and publication jobs. Only
  `sync_source_registry` has a handler, so publication stays `BLOCKED`.
- An external notification channel for Alertmanager (e-mail or chat). It needs an approved
  outbound host.
- F15.6: live tool backends for the agent roles. The agent guide lists which roles read
  live records. The other roles run on fixture backends only.
- A backup copy off the host. The backup volumes are on the same host, so a host loss is
  outside the recovery objectives. Encryption of the backup disk depends on the host.
- A serving path that loads the registry champion (no model serves decisions yet).
- Outbound allow lists and at-rest encryption settings (user decision 2026-09-30; later
  slices). A worker or ingestion writer role, when Compose gets that service. The owner
  role is still the image superuser.
- Staging drills, real data and production stores.
