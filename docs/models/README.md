# F09 baselines, F10 point model and F11 guide

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

This slice implements calibration (F11.5, F11.6, F11.7 and part of F11.8). The tabular
model and the stacker are described in the next section.

| Module | Purpose |
|---|---|
| `contracts.py` | `CalibratorArtifact`, `CalibrationTrial` and `CalibratedPrediction` |
| `calibrate.py` | `fit_calibrator`, `fit_calibrator_set`, `apply`, `calibrate` and `calibrate_with_set` |
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

- Calibrator bootstrap (F11.6): with `bootstrap_draws > 0`, the selected method is
  refitted on week-block resamples of the window (ISO week of the prediction cutoff).
  The draws, the failed count, the block count, the seed and the level are stored in the
  artifact and its hash. `calibrate` maps the base bounds through every draw and takes
  the `(1 - level) / 2` quantiles. Without a base spread, the raw probability is mapped.
  The result then includes calibrator fit uncertainty. It is still not a confidence
  interval for the true probability. One week or fewer than two converged draws give
  `BLOCKED`.
- Tour calibrators (F11.5): `fit_calibrator_set` fits a pooled calibrator on all rows and
  one calibrator per tour with the same row minimums. A tour below them, or whose fit
  fails, is recorded in `skipped` with its reason and uses the pooled calibrator.
  `calibrate_with_set` picks the tour calibrator when one exists. The tour must be known
  before the match. The prediction names the calibrator that was used.

F12 reads a `CalibratedPrediction` through `pricing/model_input.py`, so the calibration
gate can pass. A promotion registry and the MOD-03/MOD-04 comparisons on real data are
not implemented.

See [MOD-03 calibration evidence](evidence/MOD-03-calibration.md).

## F11 tabular model and stacker (`models/tabular/`)

Library: `xgboost-cpu` 3.4.1, the CPU-only XGBoost build (about 5 MB, no CUDA). The full
`xgboost` wheel pulls NCCL on Linux. `xgboost-cpu` has no macOS wheel; macOS builds from
the source distribution.

| Module | Purpose |
|---|---|
| `folds.py` | Chronological inner folds inside one training period, grouped by match |
| `schema.py` | Feature schema, missing flags, text-code indicators, market input |
| `booster.py` | `train_tabular` with nested tuning, `predict_tabular`, `TabularArtifact` |
| `stacker.py` | Stacker fit on out-of-fold rows, leakage check, `stack` |

Harness adapters are in `backtesting/ensemble.py`: `TabularCandidate` and
`StackedCandidate`.

Rules:

- **Target and rows (F11.1):** canonical player one wins. The harness passes only
  completed, retired and defaulted results whose label was known at the training cutoff.
  A row or label after the cutoff, mixed feature sets, or fewer than 40 matches (candidate
  value) give `BLOCKED`.
- **Schema (F11.1):** built from training rows only. Numbers become columns, and a missing
  number is NaN. A feature missing in any training row also gets a `missing:<name>` flag.
  Text codes such as `match.tour` get one indicator per level seen in training.
- **Player swap (F11.3):** every training row is also added in the reversed orientation
  (`swap_values`) with the reversed label. Prediction averages both orientations:
  `(f(x) + 1 - f(swap(x))) / 2`. A swap therefore gives exactly `1 - p`.
- **Nested tuning (F11.2):** in each outer walk-forward fold, every grid entry is scored on
  three inner folds of that fold's training rows. An inner fold trains only on rows with a
  cutoff before its block and labels known by then. Log loss is the objective. Brier and
  a calibration slope are diagnostics. The lowest mean log loss wins; a tie keeps the
  earlier entry. The grid may not exceed `search_budget`, and every trial is stored.
  ROI is not an objective.
- **Monotone constraints (F11.3):** the grid has each shape with and without
  non-decreasing constraints on `diff.elo`, `diff.surface_elo`, `diff.form` and the market
  input. The comparison is recorded; nothing assumes the constraint helps.
- **Market input (F09.8):** optional `market(match_id, as_of)` gives the consensus
  probability known at the cutoff. It enters as `diff.market_logit` with a missing flag.
  Run the model with and without it in one harness run to measure what the market adds.
- **Stacker (F11.4):** `p = sigmoid(sum_i w_i * logit(p_i))` with no intercept and an L2
  penalty. Its rows come only from inner folds: each component is fitted before the inner
  cutoff and predicts later matches. A component that is `BLOCKED` in an inner fold, or
  unsupported at scoring time, counts as missing (`logit = 0`), and the stack output is
  then `SPARSE`. `fit_stacker` raises `StackingLeakage` for a row whose component saw the
  match, was fitted after the row's cutoff, or whose cutoff is after the training cutoff.
- **Reproducibility (F11.8, part):** training uses one thread and a fixed seed, so a refit
  gives the same booster bytes. The artifact stores the booster JSON, its SHA-256, the
  library version, the schema, the selected parameters and every trial. `load_booster`
  refuses bytes that do not match the hash.
- **Uncertainty:** the tabular output has no spread. F12 needs a spread, so pass the output
  through a calibrator with a bootstrap (F11.6). Without one, F12 gives no assessment.

Limitations: the grid values, the fold count and the row minimums are candidate values.
All results are synthetic; see [MOD-04 evidence](evidence/MOD-04-tabular.md). There is no
promotion registry, no signed artifact, and no evaluation on approved real data.