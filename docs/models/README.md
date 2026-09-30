# F09 baselines and F10 point model guide

## F09 baselines (`models/baselines/`)

| Baseline | Input | Model |
|---|---|---|
| `baseline-ranking` | `-diff.log_rank` | `p1 = sigmoid(beta * x)`, ridge fit, no intercept |
| `baseline-global-elo` | `diff.elo` | same; initial `beta = ln(10) / 400` |
| `baseline-surface-elo` | `diff.surface_elo` | same |
| market consensus | F05 `CanonicalQuote` pairs | proportional de-vig, freshness-weighted logit mean |

There is no intercept because canonical player order is arbitrary. So `p(-x) = 1 - p(x)`
and the player-swap test checks complementarity within `2e-9` after rounding.

Rules:

- `train` accepts a row only if its snapshot cutoff and its label observation are both at
  or before the training cutoff. Labels come from `label_known_at`, not from later
  corrections. Rows from different feature-set versions cannot be mixed. No supported row
  means the fit is `BLOCKED` (`ValueError`).
- `predict` returns `UNSUPPORTED` with no probability for a missing input or an unsupported
  format (only `BEST_OF_3` in the first slice), and `SPARSE` when a player has fewer than
  `min_support_matches` rated matches. A prediction whose cutoff precedes the training
  cutoff is rejected as in-sample.
- Every prediction records the model version, artifact hash, feature set, snapshot hash,
  cutoff, prediction time and training cutoff. Probabilities are raw; `calibrated` is
  always `False`. Calibration belongs to F11.
- Uncertainty is a week-block bootstrap of the one coefficient (`WEEK_BLOCK_BOOTSTRAP`,
  default 90% spread). It is labelled as not a confidence interval for the true win
  probability and it ignores feature and rating noise.
- `report.evaluate` gives log loss, Brier, calibration intercept/slope, accuracy (secondary)
  and counts overall and per tour/surface. It rejects labels known at prediction time and
  counts unsupported rows. F13 owns the full chronological harness.
- `storage.write_baseline` writes an immutable artifact, manifest and model card. It is
  not a promotion registry.
- Consensus pairs only OPEN quotes from one bookmaker and one match, one per player,
  observed by the cutoff and within `max_pair_gap`. Cross-bookmaker, incomplete, suspended,
  invalid-odds (`<= 1`) and stale pairs are rejected and counted. A single bookmaker gives
  `SPARSE`; no pair gives `UNSUPPORTED`.

Limitations: parameters, weights and windows are candidate values. The baselines have
only synthetic results. They are shadow candidates, not evidence that any model beats the
market. F13 runs the baselines through `backtesting.runner.BaselineCandidate` and the
consensus through `backtesting.market.ConsensusCandidate`, which reads F05 quote history
known at each cutoff. F09.8 compares a model with and without the consensus as an input;
see the F11 stacker.

See [MOD-01/SYS-09 evidence](evidence/F09.md).

## F10 point model (`models/point/`)

| Module | Purpose |
|---|---|
| `formats.py` | Versioned `MatchFormat`; `enabled()` returns only verified formats |
| `exact.py` | Exact game, tiebreak, set and match distributions |
| `simulate.py` | Independent point-by-point Monte Carlo with seed and standard error |
| `model.py` | Regularized serve/return model, fit and `predict` with parameter draws |

Enabled formats: `bo3-tb7-v1` (7-point tiebreak in every set) and `bo3-final-tb10-v1`
(10-point tiebreak at 6-6 in the final set). `bo3-match-tb10-v1` and `bo5-final-tb10-v1`
are defined but not verified; best-of-five stays gated by F17. Advantage final sets are
rejected. A caller must pass the verified format of the tournament; `fmt=None` abstains.
`match_format(store, match_id, as_of)` reads the F04 deciding-set rule known at the cutoff
and returns the verified format, or raises `UnsupportedFormat` so the caller abstains.

Solver rules:

- Deuce and tied tiebreak states use closed forms, so there is no truncation. A tie that
  can never resolve (both players never lose serve) raises `NonConvergence`.
- Tiebreak service order is A, B, B, A, A, ...; the next set starts with the other player
  when the previous set had an odd number of games (a tiebreak counts as one game).
- An unknown first server averages both options. Mass sums to one within `1e-12`.
- Correct score, total games and game margin come from the same state model. They stay
  internal until F17 qualifies more markets.

Point model: `logit P(i wins a service point vs j) = mu[tour:surface] + s_i - r_j`, with
recency weights (half-life 180 days) and an L2 prior on effects (precision 10). The fit uses
only stats known at the training cutoff. `predict` returns the central probability and a
seeded spread from diagonal Laplace draws, labelled as not a confidence interval. Sparse
players (weighted points below `min_weighted_points`) abstain. The model is
`EXPERIMENTAL`: F10.7 (comparison with surface Elo) runs through the F13 harness
(`backtesting.runner.PointCandidate`). The synthetic run fails the calibration gate; see
[F13 evidence](../backtesting/evidence/F13.md). Approved held-out data is still needed.

See [MOD-02 evidence](evidence/MOD-02.md).

## F11 calibration slice (`models/calibration/`)

This slice implements calibration only (F11.5, F11.7 and part of F11.6/F11.8). The
gradient-boosting model, the stacker and nested tuning (F11.1 to F11.4) are not
implemented. They need the F13 harness and a selected tabular library.

| Module | Purpose |
|---|---|
| `contracts.py` | `CalibratorArtifact`, `CalibrationTrial` and `CalibratedPrediction` |
| `calibrate.py` | `fit_calibrator`, `apply` and `calibrate` |
| `storage.py` | Immutable `calibrator.json` with a manifest; `read_bundle` checks the base hash |

Rules:

- Two symmetric methods: `PLATT_SYMMETRIC` (`sigmoid(a * logit(p))`, no intercept) and
  `ISOTONIC_SYMMETRIC` (pool-adjacent-violators on the rows and their mirror images).
  Both give `f(1 - p) = 1 - f(p)`, because player order is arbitrary. A symmetric beta
  calibration equals symmetric Platt, so it is not a separate method.
- The window starts after the base training cutoff. Otherwise the fit is rejected as
  leakage. The fit part ends at `validation_start` and uses only labels observed by then.
  The validation part uses labels observed by `window_end`.
- Every method is scored on the validation part and recorded as a trial. The lowest
  validation log loss wins (tie: Platt). The winner is refitted on the whole window.
- Only `SUPPORTED` rows are used. Duplicate matches, rows outside the window, labels known
  at prediction time and mixed base artifacts are rejected. Too few rows give `BLOCKED`
  (defaults: 50 fit and 30 validation rows; candidate values).
- Output is clipped to `[0.01, 0.99]` (candidate value).
- `calibrate` refuses a prediction from another base artifact or training cutoff, and a
  prediction inside the calibration window. The caller must then abstain; there is no
  silent fallback to the raw probability.
- A `CalibratedPrediction` sets `calibrated = True`. Its spread is the base bootstrap
  spread mapped through the calibrator. It excludes calibrator fit uncertainty and it is
  not a confidence interval for the true probability. Its `model_ref` names the
  calibrator version and hash, which include the base artifact hash.

F12 reads a `CalibratedPrediction` through `pricing/model_input.py`, so the calibration
gate can pass. Tour-specific calibrators, calibrator bootstrap, a promotion registry and
the MOD-03/MOD-04 comparisons on real data are not implemented.

See [MOD-03 calibration evidence](evidence/MOD-03-calibration.md).