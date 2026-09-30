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

## Not implemented

- A scheduler that runs the signal producers and `evaluate-alerts` on a cadence.
- A Prometheus server, dashboards and an alert manager.
- F15.6 agent tool scoping. It depends on F18.
