# SYS-06 evidence — F06 payout, settlement and ledger

Date: 2026-09-29. Scope: synthetic fixtures only. Reviewer approval of real rules: pending.

## What is verified

| Check | Test | Result |
|---|---|---|
| Registry fails closed (missing, draft, suspended, expired, not yet known, out of interval, kind/bookmaker mismatch, evidence) | `tests/test_settlement_rules.py` | pass |
| Reviewed rules state every semantic field | `tests/test_settlement_rules.py` | pass |
| Hand-calculated payout cases to PLN 0.01, including the threshold boundary and payout cap | `tests/test_pricing_payout.py` | pass |
| Preview precedence, exact binding, mismatch review, stake limits, promotions | `tests/test_pricing_payout.py` | pass |
| Payout properties: cent-exact, bounded by `stake * odds`, deterministic | `tests/test_pricing_payout_properties.py` | pass |
| Every synthetic settlement branch, missing branches, draft real rules stay `PENDING` | `tests/test_settlement_engine.py` | pass |
| Ledger idempotency, corrections, pending exposure, concurrency, reconciliation property | `tests/test_settlement_ledger.py` | pass |
| Migration compiles offline with one head | `tests/test_foundation_migrations.py`, `scripts/check_migrations.py` | pass |
| PostgreSQL append-only triggers and advisory-lock serialization | `tests/integration/test_settlement_persistence.py` | pass (2026-09-30, isolated `tennis_track_2`) |

## Synthetic payout golden cases

Synthetic regime (not law): stake tax 10% (half up), gross return rounded down, winnings
tax 20% of the gross return when it is strictly above PLN 1000.00, cap PLN 100000.00.

| S | Odds | Effective stake | Gross | Winnings tax | W |
|---|---|---|---|---|---|
| 100.00 | 2.50 | 90.00 | 225.00 | 0.00 | 225.00 |
| 3.33 | 1.87 | 3.00 | 5.61 | 0.00 | 5.61 |
| 444.44 | 2.50 | 400.00 | 1000.00 | 0.00 | 1000.00 |
| 444.45 | 2.50 | 400.01 | 1000.02 | 200.00 | 800.02 |
| 500.00 | 2.50 | 450.00 | 1125.00 | 225.00 | 900.00 |
| 5000.00 | 30.00 | 4500.00 | 135000.00 | 27000.00 | 100000.00 (capped) |

## Not yet demonstrated

- Reviewer-labelled fixtures for each real bookmaker rule branch.
- Comparison of captured coupon previews against the calculator.
- Duplicate and correction replay against PostgreSQL.
