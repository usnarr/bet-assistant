# F15 operations and security guide

F15 makes the service observable and recoverable. It stops publication when its inputs or
controls cannot be trusted. Deterministic code owns every stop. No code path places a bet.

## Status

Demonstrated on synthetic fixtures and isolated local services:

- F15.3 metrics: an in-process registry with the Prometheus text format at `GET /metrics`.
- F15.3 signals: freshness, parser drift, missing-value rate, probability drift (PSI) and
  ledger balance.
- F15.4 versioned alert rules (`configs/operations/alert-rules.json`, status `PROPOSED`).
- F15.4 deterministic controls: a critical alert stops the affected source, or turns the
  global stop on, through the F01 journal.
- F15.1 job graph for the blueprint section 33.1 jobs, dependency and publication gates,
  separate backfill capacity, and idempotent job runs.
- F15.2 resource leases with expiry and fencing tokens (migration `0011_operations`).
- F15.5 security: read-only API mounts and root file system in Compose, a SELECT-only
  database role for the API, token digests only, placeholder refusal in production, and
  a source scan for unsafe deserialization.
- F15.7 restore verification: database and journal fingerprints, raw-object and ledger
  reconciliation, and incident bundles with affected recommendation IDs.
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

No Prometheus server, Grafana dashboard or alert manager is part of the repository. The
blueprint names them as options. They need a concrete deployment first.

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

The thresholds are proposals. Blueprint section 34.5 and the F05 parser-drift proposal are
their basis. They are not tuned on real data. Change a threshold only with a new version
and a reason.

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

A `PROPOSED` rule set can apply controls. A stop is the safe direction, so this needs no
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

The job graph and runner are ready for a scheduler. No scheduler runs them yet. ADR 0002
selects Prefect, but it is not installed. It needs a concrete deployment first.

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
| API process | serves the above | | | read-only SQLite connection, read-only mount | `SELECT` only with the reader role |
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

### Separate database role

The API can use a SELECT-only role through `TENNIS_SERVING_DATABASE_URL`. An administrator
creates the login role, then grants read access:

```powershell
docker compose exec postgres psql -U tennis -d tennis -c "CREATE ROLE tennis_reader LOGIN PASSWORD '<secret>'"
uv run tennis-ops grant-reader --role tennis_reader
```

Run `grant-reader` again after each migration, because new tables need the grant. The role
cannot insert, update, delete or create tables. An integration test checks this. The
ingestion and pricing writers still use the main role. Separate writer roles per
component are pending.

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

Back up three stores:

| Store | Backup | Verify |
|---|---|---|
| PostgreSQL | `pg_dump -Fc` | `tennis-ops fingerprint` before, `tennis-ops verify-restore` after |
| F01 journal | `tennis-ops backup-journal` (SQLite online backup) | journal fingerprint, hash of every row and document |
| Raw objects | Copy or replicate the bucket | `reconcile_raw_objects`: each archived content exists and matches its SHA-256 |

`verify-restore` compares row counts and ordered-row digests of every `tennis` table, the
Alembic revision and, when given, both journals. The ledger check confirms that every
virtual ledger balances.

Recovery objectives (RTO and RPO) are not agreed. Without them the report is `BLOCKED`,
even when integrity passes. This blocks operational readiness, as F15 requires. Pass
`--rto-seconds` and `--rpo-seconds` after the owner agrees them.

The drill `scripts/ops02_restore_drill.py` runs the full cycle on a disposable test
container. See [OPS-02](evidence/OPS-02.md).

### Model rollback

No model registry or serving model selection exists yet. F13 promotion decisions record a
rollback reference, and model artifacts are hash-checked. A rollback drill is pending until
a model serves decisions.

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

- A scheduler that runs the jobs, the signal producers and `evaluate-alerts` on a cadence.
- A Prometheus server, dashboards and an alert manager.
- F15.6: a separate database role for agent writes. The agent store uses the main role.
- F15.6: live tool backends for the agent roles. The agent guide lists which roles read
  live records. The other roles run on fixture backends only.
- Agreed RTO and RPO. Continuous WAL archiving (point-in-time recovery).
- Model rollback drill (no model registry yet).
- Separate writer roles per component, outbound allow lists and at-rest encryption
  settings. These depend on the deployment.
- Staging drills, real data and production stores.
