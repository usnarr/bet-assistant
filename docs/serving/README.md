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

Not implemented or pending:

- F14.6 explanation agent. It depends on F18 and its evaluation gates.
- Production wiring. `create_app()` without a `Serving` argument returns HTTP 503 on
  every F14 route. The host must build the service from approved stores (see below).
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
| `decision` | `BET`, `WATCH`, `NO_BET`; repeat for more | Matches the recorded decision |
| `starts_after`, `starts_before` | ISO 8601 with offset | Half-open interval on scheduled start |
| `limit` | 1 to 100, default 50 | |
| `cursor` | the `next_cursor` of the previous page | Bound to the same filters |

Order is scheduled start, then recommendation ID. The order is stable across pages.

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
| 503 | `SERVICE_NOT_CONFIGURED` | The host did not supply the F14 service |

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

## Host wiring

The host builds the service and passes it to `create_app`:

```python
service = RecommendationService(
    store=PostgresDecisionStore(engine),
    checks=GovernanceReadChecks(per_thread(open_governance), history_actionability(history, policy)),
    clock=SystemClock(),
    redistribution=GovernanceRedistribution(per_thread(open_governance)),
)
app = create_app(settings, serving=Serving(service, TokenAuthenticator(load_credentials(path))))
```

`open_governance` opens a `GovernanceService` on the governance journal with a read-only
principal. Settings for these paths do not exist yet. They belong to F15 operations.

## Latency

A local in-process sample is in the evidence file. It is not a staging measurement. The
proposed target (p95 below 500 ms at the staging workload) is not yet measured.
