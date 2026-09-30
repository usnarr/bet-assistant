# SYS-05 evidence — F05 bookmaker adapters

Date: 2026-09-29. Scope: synthetic fixture shapes only.

## Fixtures

`tests/fixtures/bookmakers/generate.py` writes two polls per bookmaker from shared
scenarios. The expected files come from the scenarios, not from the parsers. Each poll has
22 events: 20 accepted and 2 rejected (missing start; a start without a UTC offset, or for
Fortuna a Warsaw time repeated at the daylight-saving change). Fortuna reads a comma as its
normal decimal separator, so its malformed-odds scenario is valid there. They cover
reversed participants, a suspended selection, a suspended market, a moved start (poll 2),
a price change (poll 2), malformed odds, odds not above 1, an unsupported market, doubles,
best of five, a started event, a cancelled event, a closed market and a promotion marker.

## Verified

| Check | Test | Result |
|---|---|---|
| Golden events and quotes match the independent expectations; exact decimal text kept | `tests/test_bookmaker_adapters.py` | pass (Betclic, Superbet, Fortuna) |
| Schema drift raises and F03 dead-letters the content | `tests/test_bookmaker_adapters.py` | pass (Betclic, Superbet, Fortuna) |
| Drift metrics and alerts | `tests/test_bookmaker_adapters.py` | pass (Betclic, Superbet, Fortuna) |
| Repository source policy keeps the adapter disabled (`SOURCE_UNKNOWN`, `SOURCE_DISABLED`) | `tests/test_bookmaker_adapters.py` | pass (Betclic, Superbet, Fortuna) |
| Reversed listings map to the same canonical player | `tests/test_bookmaker_adapters.py`, `tests/test_bookmaker_mapping.py` (real F04 resolver) | pass |
| No stale, suspended, closed, started, cancelled, withdrawn or unconfirmed quote is actionable | `tests/test_bookmaker_quotes.py`, `tests/test_bookmaker_adapters.py` | pass |
| Property: an actionable quote is fresh, confirmed, pre-start and expires later | `tests/test_bookmaker_quotes.py` | pass |
| Later observations cannot make an earlier decision fresh | `tests/test_bookmaker_quotes.py`, `tests/test_bookmaker_history.py` | pass |
| Idempotent append-only history | `tests/test_bookmaker_history.py` | pass |
| PostgreSQL history store | `tests/integration/test_bookmaker_history_persistence.py` | pass (2026-09-30, isolated `tennis_track_2`) |

## Not demonstrated

- The 99% valid and under 1% unresolved-label rates over a stable seven-day collection.
- Any real Betclic, Superbet or Fortuna payload.
- Adaptive polling cadence (F05.5) and a kill-switch drill.
