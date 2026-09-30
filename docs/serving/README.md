# F14 recommendation API and read-only dashboard guide

F14 shows stored F12 decisions to people. It never makes or changes a decision. Every
record is virtual shadow output. No code path places a bet.

Evidence: [SYS-12](evidence/SYS-12.md).

## Status

Demonstrated on synthetic fixtures:

- F14.1 typed views, filters, keyset pagination and explicit error codes.
- F14.2 token authentication and server-side role permissions.
- F14.3 and F14.4 server-rendered dashboard: list, match detail and audit pages.
- F14.5 deterministic explanations.
- F14.7 responsible-use status and the manual quote-confirmation notice.
- F14.8 read-time rechecks of expiry, supersession, kill switches and quote state.
- PostgreSQL decision store (migration `0010_decision_records`).
- Production wiring from typed settings (see "Production wiring"). The Compose API uses it.

Not implemented or pending:

- F14.6 explanation agent. It depends on F18 and its evaluation gates.
- A browser and assistive-technology review of the dashboard. Only automated structure
  checks exist.
- Redistribution approvals. No source has a reviewed redistribution right.
- Legal review of which derived numbers a viewer may see (see "Redistribution").
- Real data. All fixtures are synthetic.

## Routes

All F14 routes are `GET` only. Each route needs a token. Each route sets
`Cache-Control: no-store`.

| Route | Permission | Content |
|---|---|---|
| `GET /v1/tennis/recommendations` | read recommendations | Current or historical views |
| `GET /v1/tennis/matches/{match_id}/analysis` | read analysis | Comparison, components, decisions, quotes, explanations |
| `GET /v1/audit/recommendations/{id}` | read audit | Stored record and context, unchanged |
| `GET /v1/tennis/source-health` | read recommendations | Source state and quote freshness |
| `GET /dashboard` | read recommendations | HTML list with filters |
| `GET /dashboard/matches/{match_id}` | read analysis | HTML match detail |
| `GET /dashboard/audit/{id}` | read audit | HTML audit view |

### List filters

| Parameter | Values | Notes |
|---|---|---|
| `view` | `current` (default), `history` | See "Current and history views" |
| `bookmaker` | a supported bookmaker ID | Default set: `betclic`, `superbet`, `fortuna` |
| `market` | `match_winner` or `TENNIS_MATCH_WINNER` | Other markets return `UNSUPPORTED_MARKET` |
| `decision` | `BET`, `WATCH`, `NO_BET`; repeat for more | Matches the served (effective) decision |
| `recorded_decision` | `BET`, `WATCH`, `NO_BET`; repeat for more | Matches the stored decision |
| `starts_after`, `starts_before` | ISO 8601 with offset | Half-open interval on scheduled start |
| `limit` | 1 to 100, default 50 | |
| `cursor` | the `next_cursor` of the previous page | Bound to the same filters |

Order is scheduled start, then recommendation ID. The order is stable across pages.

### Decision filter semantics

`decision` filters on the `decision` field of the response: the decision after the
read checks. In the current view, a `BET` that a read check blocks is served as `NO_BET`.
It then matches `decision=NO_BET`, not `decision=BET`. So `decision=BET` returns only
`BET` rows that are actionable now. In the history view, the served decision is the
recorded decision, so both filters give the same result.

`recorded_decision` filters on the stored F12 decision. Use it for audit questions, for
example `recorded_decision=BET&decision=NO_BET` lists stored BETs that are blocked now.
The audit route always shows the stored record unchanged.

Reason for this choice: F14 says that current lists revalidate expiry and hard stops, and
that historical audit reads keep the original decision. A current filter on the stored
value would return a blocked record under `decision=BET`. That is misleading.

The store can filter only on the stored decision. The service therefore reads candidate
rows and rechecks each one. One request reads at most 1000 rows (`MAX_SCAN`). When it
reaches this limit, the page can be shorter than `limit` and still have a `next_cursor`.
Follow the cursor until it is `null`.

### Errors

Every error body is `{"error": {"code": ..., "detail": ...}}`.

| Status | Code | Cause |
|---|---|---|
| 401 | `AUTHENTICATION_REQUIRED` | No token or an unknown token |
| 403 | `PERMISSION_DENIED` | The role does not have the permission |
| 404 | `MATCH_NOT_FOUND`, `RECOMMENDATION_NOT_FOUND` | No stored decision |
| 405 | (framework) | Any method other than `GET` |
| 422 | `INVALID_FILTER` | A malformed value, a naive time, a reversed range, a bad limit or ID |
| 422 | `INVALID_CURSOR` | A malformed cursor or a cursor from other filters |
| 422 | `UNSUPPORTED_MARKET`, `UNSUPPORTED_BOOKMAKER` | Out of scope |
| 503 | `SERVICE_NOT_CONFIGURED` | Serving is off (`TENNIS_SERVING_ENABLED=false`) |
| 503 | `DEPENDENCY_UNAVAILABLE` | The decision store or another required store failed |

## Views and numbers

- Money, odds and probabilities are decimal strings in JSON. They are never JSON numbers.
- `recorded_decision` is the stored decision. `decision` is the effective decision now.
- `recommended_stake` is positive only for an actionable `BET`. `recorded_stake` keeps
  the stored value.
- An unavailable metric is `null`. It is never `0`.
- Each view has `generated_at`, `quote_observed_at`, `quote_age_seconds`, `expires_at`,
  `failed_gates`, `reasons`, `read_time_reasons` and `policy_versions`.
- Each view has `mode: SHADOW`, `virtual: true`, `manual_quote_confirmation_required: true`
  and `automated_placement: false`.

### Current and history views

The current view shows only records that are not expired and not superseded. Every
current read runs the read checks again. There is no cache.

A read check can fail. Then the served view shows `decision: NO_BET`,
`actionable: false`, a zero recommended stake and the reasons in `read_time_reasons`. A
`WATCH` becomes `NO_BET` in the same way. The stored record does not change.

The history view shows every stored version with its original decision. A history view
is never actionable.

The audit route returns the stored record exactly as written. It also shows the version
chain, the successors and each source's redistribution right. It never returns raw
source payloads, only hashes and IDs.

## Read checks (F14.8)

`GovernanceReadChecks` runs these checks for each current record:

1. Each lineage source is enabled for the configured purpose (kill switch, suspension,
   approval, review expiry).
2. The bookmaker payout policy is approved and in effect.
3. The responsible-use policy allows recommendations (global disable, cooling-off,
   account disable).
4. For a `BET` only: the F05 quote is actionable, not expired, and at the same price.
   This uses `volatile_problems`, the same function as F12 `prepare_publication`.

The service also rejects an expired or superseded record. A check that raises an error
gives `READ_CHECK_UNAVAILABLE`. The record is then not actionable.

SQLite connections cannot cross threads. Pass a `per_thread(factory)` governance factory.

## Roles and authentication (F14.2)

| Role | Recommendations and analysis | Audit | Values from sources without redistribution rights |
|---|---|---|---|
| `dashboard` | yes | no | withheld |
| `agent` | yes | no | withheld |
| `operator` | yes | yes | shown |
| `policy_reviewer` | yes | yes | shown |

- Send `Authorization: Bearer <token>`. A browser can use HTTP Basic with the token as
  the password. The user name is ignored.
- The server never accepts a token in the query string.
- The credential file is a JSON list of `{"identity", "role", "token_sha256"}`. It holds
  digests only. Load it with `load_credentials(path)`.
- No F14 route writes. No role can change identities, policy, funds, models or risk
  settings through F14.

## Redistribution

`GovernanceRedistribution` allows a source only when F01 approves it for the
`redistribution` purpose. The default `NoRedistribution` allows no source.

For viewer roles, the service withholds quoted odds and evidence fact values from a source
without that right. The engine's own numbers stay visible: probabilities, break-even,
expected value and stake. These numbers can let a viewer estimate the price. This rule is
a proposal. It needs a legal and licence review before any external viewer gets access.

## Explanations (F14.5)

`explain()` builds sentences from the stored record and context only. Each number is the
exact stored `Decimal` string. Each sentence has a kind:

| Kind | Meaning |
|---|---|
| `OBSERVED` | A stored fact, for example a quote or a match count |
| `INFERRED` | A model estimate, for example a rating or a probability |
| `DERIVED` | Deterministic payout or value arithmetic |
| `MISSING` | No verified value exists |
| `WITHHELD` | The viewer may not see the value |
| `DECISION` | A failed gate or the final decision |

The templates make no claim about motivation, injuries, certainty or profit. A `BET`
sentence says to confirm the quote manually and says that the system places no bets.

## Dashboard (F14.3, F14.4)

The dashboard is server-rendered HTML with one stylesheet. It has no JavaScript. The
only form is a `GET` filter form. The Content Security Policy blocks scripts.

Display formatting:

| Value | Display |
|---|---|
| Money, odds | Exact stored string |
| Probability, break-even | Percent, two decimals, half-even rounding |
| Expected value | Floored to whole grosze |
| Return (ROI) | Percent, two decimals, floored |
| Missing value | "not available" |

Each formatted number has the exact stored value in its `title` attribute. `BET`,
`WATCH` and `NO_BET` use the same badge style. Every row lists its reasons in plain text.
A decision blocked at read time shows "recorded BET; blocked at read time".

## Storage

`InMemoryDecisionStore` is for tests. `PostgresDecisionStore` uses table
`tennis.decision_record` from migration `0010_decision_records`.

- The table is append-only. A trigger rejects `UPDATE` and `DELETE`.
- Check constraints reject a positive stake on `WATCH` or `NO_BET` and an expiry before
  the decision time.
- A correction is a new F12 version with `supersedes`. The old version stays.
- An identical retry is a no-op. Other content under the same ID raises
  `DecisionConflict`.

## Production wiring

The process entry point is `tennis_engine.serving.wiring:create_production_app`. The image
starts it with `uvicorn --factory`. It reads these settings:

| Variable | Default | Meaning |
|---|---|---|
| `TENNIS_SERVING_ENABLED` | `false` | Off: every F14 route returns 503 |
| `TENNIS_API_CREDENTIALS_FILE` | none | Token digest file. Required when serving is on |
| `TENNIS_GOVERNANCE_JOURNAL` | none | F01 journal. Required when serving is on |
| `TENNIS_SERVING_DATABASE_URL` | `TENNIS_DATABASE_URL` | Optional read-only database role |
| `TENNIS_SERVING_ACCOUNT_SCOPE` | `shadow` | Responsible-use scope for read checks |
| `TENNIS_SERVING_STALE_AFTER_SECONDS` | `300` | Source-health stale limit |
| `TENNIS_SERVING_CONNECT_TIMEOUT_SECONDS` | `3` | Database connection limit |

The factory builds the service from these stores:

- `PostgresDecisionStore` for decisions.
- `PostgresHistoryStore` and `QuoteHistory` for F05 quote actionability, with the
  proposed `ActionabilityPolicy`.
- The F01 journal, opened read-only for each thread with a viewer principal. SQLite
  rejects every write on this connection.
- `GovernanceRedistribution` on the same journal.

### Fail-closed behaviour

| Condition | Result |
|---|---|
| Serving off | 503 `SERVICE_NOT_CONFIGURED` on every F14 route |
| Serving on, a path setting missing | Settings validation fails; the process does not start |
| Production: placeholder database or object-store secret | The process does not start |
| Production: credential file missing, empty, invalid or with a placeholder token | The process does not start |
| Production: journal missing or not initialized | The process does not start |
| Development: credential file missing | Every F14 request gets 401 |
| Journal fails at read time | Each record is `NO_BET` with `READ_CHECK_UNAVAILABLE` |
| Quote store fails or has no observation for a `BET` | That `BET` is served as `NO_BET` |
| Decision store fails | 503 `DEPENDENCY_UNAVAILABLE` |

`/health/ready` adds a `governance_journal` check when serving is on. It shows only
`read-only; global_stop=on|off`, never a path.

### Tokens

`tennis-platform create-api-token --identity <name> --role <role>` adds a random token. It
prints the plain token once. The file keeps only its SHA-256 digest. Use `--rotate` to
replace a token and `revoke-api-token --identity <name>` to remove it. The API reads the
file at start, so restart the API after a change.

### Compose

Compose turns serving on. `governance-init` creates an empty journal. An empty journal
denies every permission, and the global stop is on. The API mounts the journal and the
credential volume read-only, and its root file system is read-only. Operator commands use
the `admin` profile:

```powershell
docker compose run --rm admin tennis-platform create-api-token --identity ops --role operator
docker compose restart api
```

## Latency

A local in-process sample is in the evidence file. It is not a staging measurement. The
proposed target (p95 below 500 ms at the staging workload) is not yet measured.
