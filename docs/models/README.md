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
market. F09.8 (with/without market inputs) waits for F11/F13 comparisons.

See [MOD-01/SYS-09 evidence](evidence/F09.md).
