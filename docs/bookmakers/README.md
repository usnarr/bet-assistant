# F05 bookmaker adapters and quote freshness guide

F05 turns bookmaker responses into timestamped, attributable quotes, maps them to canonical
matches through F04, and decides whether a quote is actionable. It never places a bet.
Every bookmaker source in the F01 register is `DRAFT` with the kill switch on, so no
adapter can collect real data yet.

## Status per bookmaker

| Bookmaker | Parser version | Source policy | State |
|---|---|---|---|
| Betclic | `betclic-synthetic-v1` | `betclic-odds` (DRAFT, killed) | Parser, fixtures and end-to-end tests on a synthetic shape |
| Superbet | `superbet-synthetic-v1` | `superbet-odds` (DRAFT, killed) | Parser, fixtures and end-to-end tests on a synthetic shape |
| Fortuna | `fortuna-synthetic-v1` | `fortuna-odds` (DRAFT, killed) | Parser, fixtures and end-to-end tests on a synthetic shape |

All three payload shapes are invented. Each parser keeps its own assumptions: Betclic
uses ISO starts with an offset; Superbet uses epoch-millisecond UTC starts and bare JSON-number
prices; Fortuna uses Warsaw wall-clock starts (a time that is repeated or skipped at a
daylight-saving change is rejected), comma decimals and home/away positions. F05.1 requires an approved access path, quotas and a
one-event comparison with the source presentation. Then the parser gets a new version for
the real shape. Do not approve a source only to exercise an adapter.

## Flow

1. The F03 approved fetcher collects a response. It rechecks the F01 policy at execution
   time. F03 archives the bytes before parsing.
2. The adapter is the F03 `SourceParser`. A schema mismatch raises `SchemaDrift`. F03 then
   dead-letters the raw content. A bad record (malformed odds, a missing start, a start
   without a UTC offset, an unknown status) becomes a `RejectedRecord`. It is counted and
   kept, never dropped silently.
3. `Snapshot.observe` adds the F03 observation time and raw hash. The payload never
   supplies the observation time.
4. `map_snapshot` resolves each event through the F04 `IdentityResolver`. Each selection
   maps to the player that F04 resolved for that source participant, so source order never
   decides the canonical outcome. Doubles, best-of-five, unknown starts, unresolved events
   and unsupported markets stay unmapped with reasons. Unknown markets are kept for
   diagnostics only.
5. `QuoteHistory.record` appends the poll, each quote observation and each event mapping.
   Retries are idempotent.

## Intervals and actionability

An interval changes only when the fingerprint changes (bookmaker, event, market,
selection, line, odds, state). A repeated price is a heartbeat.

`evaluate_actionability` uses one versioned policy, `quote-actionability-proposed-v1`. The
settings are proposals, not accepted policy:

- at least 2 consecutive observations with the same price, start and event state;
- the oldest counted observation at least 20 s before the latest;
- the latest observation younger than 60 s (the limit is exclusive);
- the quote open, the event pre-match with a known start that has not passed;
- no later successful poll of the event without this selection (withdrawn).

Only observations known at the decision time count. Failed polls are not observations, so
they cannot extend a quote's life. `expires_at` never passes the scheduled start. Suspend
publication when source quotas cannot support this cadence.

## Drift metrics

`snapshot_metrics` counts events, markets, selections, rejected records, unknown labels,
missing starts and duplicate selection IDs. `drift_alerts` uses proposed thresholds
(`parser-drift-proposed-v1`): volume change over 50%, valid rate under 99%, unknown labels
over 2%, zero events, duplicate IDs. Tune them per bookmaker.

## Storage

Migration `0007_bookmaker_quotes` adds `bookmaker_poll`, `bookmaker_quote_observation` and
`bookmaker_event_mapping` with append-only triggers. The exact source decimal text stays in
the JSON payload.

## Limitations

- No real payload, access path, quota or seven-day collection exists. The SYS-05 rates
  cannot be measured.
- Adaptive polling cadence (F05.5) and the kill-switch drill are not implemented.
- Coupon-preview capture for F06 is not implemented.
- The PostgreSQL stores are tested only offline in this environment.

Evidence: [SYS-05](evidence/SYS-05.md).
