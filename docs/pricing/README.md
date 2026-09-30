# F12 value decisions and bankroll risk guide

F12 turns a mapped quote, a payout, model output and the current virtual exposure into a
`BET`, `WATCH` or `NO_BET` record. Every gate result is recorded. Records are virtual
shadow records; no code path places a bet.

## Status

- Implemented: value arithmetic, capacity and stake search, exposure reservations,
  the ordered gate sequence, decision records and the publication recheck.
- F09 wiring (`pricing/model_input.py`): `assessment_from_baselines` maps a primary F09
  baseline plus the other baselines to a `ModelAssessment`. The selected player's
  probability follows canonical order. The conservative probability is the lower edge of
  the model's bootstrap spread, a model-spread proxy and not a confidence bound.
  Disagreement is the largest gap to another supported baseline. F09 baselines are raw
  (`calibrated = False`) and trained on sporting results (`SPORTING_WIN`), so they always
  fail the calibration gate: **no F09 baseline can produce a `BET`**. That needs calibrated
  F11 output. `consensus_for_selection` confirms a large edge only from a SUPPORTED
  consensus (more than one bookmaker).
- The repository decision policy (`configs/risk/decision-policy.json`) and the
  responsible-use policy are drafts. Both block every `BET` until a reviewer approves them.

## Value (`pricing/value.py`)

For a binary cash bet where a loss returns zero: `EV = p*W - S`, `ROI = EV/S`,
`break_even = S/W`, with the same formulas for `p_low`. Conservative Kelly is
`max(0, (r*p_low - 1)/(r - 1)) * kelly_fraction` with `r = W/S`.
`outcome_expected_value` handles a complete outcome model with voids or partial returns.
`proportional_devig` normalizes one complete market.

A model output with `SPORTING_WIN` semantics does not include void outcomes. It is blocked
unless the decision policy states `void_stress_probability`. Then both probabilities are
multiplied by `1 - q`, and a stressed void is treated as a loss.

## Capacity and stake (`pricing/risk.py`)

`capacity` takes the smallest of: available cash, the single-bet bankroll fraction, the
event, bookmaker and open exposure caps, the daily/weekly/monthly stake allowances (Warsaw
calendar), and the bookmaker maximum. Count limits, open-bet limits, an empty bankroll and
the drawdown stop remove capacity. Drawdown uses equity: cash plus open stakes at cost.

`size_stake` walks the stake grid down from the cap. It returns the largest stake with
positive conservative EV at its own payout that does not exceed the conservative Kelly
stake at that payout. It recomputes the payout at every stake, so tax thresholds and caps
are never extrapolated. Below the bookmaker minimum it abstains. Past losses are not an
input; a property test checks that a lower bankroll never raises the stake.

## Reservations (`pricing/reservation.py`)

`ExposureService.reserve` recomputes capacity from the state read under a lock per ledger.
A decision key gives one reservation; a retry returns it; reuse with other terms fails.
Reservations expire, can be released, and commit to the virtual ledger with the
reservation ID as bet ID. Same-event positions add up across selections and bookmakers.
PostgreSQL storage (migration `0008_risk_reservations`) is append-only and uses an
advisory lock.

## Gates (`pricing/decision.py`)

Order: decision policy, identity, not started, quote fresh, market and selection, rules
(settlement rule, payout at the minimum stake, publication gate), feature quality, model
domain, calibration, disagreement, central EV, conservative EV, conservative ROI, outlier
confirmation, risk budget, responsible use, stake.

- Every gate runs, even after an earlier failure, so the record lists every reason.
- Any hard failure gives `NO_BET`.
- `WATCH` needs every hard gate to pass, a positive central EV and a failed conservative
  value gate.
- A stake below the bookmaker minimum with value intact is `NO_BET`.
- `BET` needs every gate. The value gates are rechecked at the final stake.
- An edge above `max_unconfirmed_edge` needs a consensus probability within
  `consensus_tolerance` of the model.

`prepare_publication` rechecks volatile gates right before publication: decision expiry,
the kill switch and publication gate, freshness and an unchanged price, and responsible
use. It then reserves the stake. A failure appends a superseding `NO_BET` version; the
original record stays unchanged.

`DecisionRecord.to_recommendation()` returns the shared domain `Recommendation` for F14.
Its `expected_value` is floored to whole grosze; the record keeps the exact value.

Evidence: [SYS-10 and SYS-11](evidence/SYS-10-11.md).
