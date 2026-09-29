# F06 payout, settlement and virtual ledger guide

F06 computes the exact cash return on a win, settles match-winner bets from reviewed
bookmaker rules, and records virtual (shadow) activity in an append-only ledger. It does
not contain real tax settings or real bookmaker rules. It never places a bet.

## Terms

- `S` is the stake deducted. `W` is the actual cash returned on a win, including any
  returned stake. `net_profit = cash_return - S`.
- A binary cash bet has `EV = p*W - S` only when a loss returns zero. F12 owns EV, stake
  sizing and decisions; F06 supplies `W` and settlement results.

## Rule registry (`tennis_engine.settlement.rules`)

Rules live in `configs/settlement/rules/*.json`. Each rule version has an owner, an
effective interval `[from, until)`, a review time, a review expiry and archived evidence.

| Kind | Purpose |
|---|---|
| `jurisdiction_payout` | Tax regime for a jurisdiction (stake tax, winnings tax, rounding) |
| `bookmaker_payout` | Stake limits, increments, payout cap, and either its own tax regime or a reference to one jurisdiction rule |
| `bookmaker_settlement` | Match-winner branches: completion, retirement, walkover, disqualification, abandonment, postponement, venue/surface/format change, wrong listing, palpable error, void return |
| `promotion` | Eligibility and validity for one promotion |

A `REVIEWED` rule must state every semantic field, even as `null`. A missing key is a
validation error, so "absent" never means "no tax" or "bet stands". A lookup fails closed
when the rule is missing, `DRAFT`, `SUSPENDED`, reviewed after the decision time, past its
review expiry, or not effective at bet time. With an evidence checker, the lookup also
verifies the archived document in the F01 governance store under the scope
`payout:rule:<version>`.

The repository configs for Betclic, Superbet, Fortuna and PL are `DRAFT` with no
semantics. Consequently every real payout is non-actionable and every real settlement is
`PENDING` until a reviewer adds reviewed rule versions. This is intended.

## Payout resolution (`tennis_engine.pricing.payout`)

`resolve_payout(request, policy_lookup, registry, preview)` uses this order:

1. A coupon preview bound to the exact bookmaker, market, selection, odds, stake,
   account scope, promotion, payout policy version and capture interval.
2. The reviewed bookmaker payout rule with its own tax regime.
3. The reviewed jurisdiction rule that the bookmaker rule references.

An unbound preview is ignored and noted (`PREVIEW_NOT_BOUND:<field>`). A preview at another
stake is never scaled. A bound preview that differs from the calculator fails closed with
`PREVIEW_MISMATCH` and `review_required`. When the calculator is unavailable, a bound preview
is still usable (`CALCULATOR_UNAVAILABLE`).

The calculation applies stake tax, gross return, winnings tax and the payout cap, rounding
only where the rule states it. The result must be exact to PLN 0.01. `research_cash_return`
(`stake * odds`) is a diagnostic only and never makes a payout actionable.

Only `STAKE_TAX_COVERED` promotions have an executable model. All other promotion kinds are
`PROMOTION_UNSUPPORTED`. Winnings tax thresholds make payouts nonlinear: a promotion can
raise the gross return above a threshold and lower the cash return. F12 must recompute `W`
at the final rounded stake.

## Settlement (`tennis_engine.settlement.engine`)

`settle(context, registry)` returns `WON`, `LOST`, `VOID` or `PENDING`. It uses the rule
effective at bet time and known at settlement time. `HALF_WON`/`HALF_LOST` are reserved for
later markets and rejected for match winner.

The bet stays `PENDING` when the rule or branch is missing, the result is disputed, the
match is not finished or is cancelled, the postponement window is open, the advancing
player is unknown, or a branch requires manual review (wrong listing, palpable error). An
agent or operator cannot invent a result to close it.

## Virtual ledger (`tennis_engine.settlement.ledger`)

- Only `VIRTUAL` ledgers are accepted. `ACTUAL` is reserved for later manual records.
- A stake debit is idempotent by bet ID. A reused bet ID with other terms is a conflict.
  A stake above the available balance is rejected.
- A settlement is idempotent by its financial digest. A `PENDING` result is recorded for
  audit and keeps exposure open.
- A different final result needs `correction_reason`. It appends a `REVERSAL` of the active
  credit plus a replacement `SETTLEMENT_CREDIT`. A final result cannot return to pending.
- `reconcile` checks sequence continuity, running balance and
  `opening + deltas = closing`, and reports open exposure.

PostgreSQL storage (migration `0004_settlement`) uses append-only triggers, unique
idempotency keys, a unique reversal per entry, and a per-ledger advisory lock.

## Rollback

Select a reviewed prior rule version for new decisions. Correct past settlements with
explicit correction entries. Never edit or delete ledger history.

## Open items

- Real Betclic, Superbet, Fortuna and PL rule reviews are external approvals. They remain
  pending.
- Coupon previews need an approved capture path (F05). No preview table exists yet.
- F12 exposure reservation and responsible-use limits build on this ledger.
- Evidence: [SYS-06](evidence/SYS-06.md).
