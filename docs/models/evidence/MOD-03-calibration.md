# MOD-03 calibration evidence — F11 calibration slice

Date: 2026-09-30. Scope: synthetic predictions and labels only. No real data, no promotion.
This covers the calibration part of MOD-03. The candidate-versus-baseline comparison
(MOD-03/MOD-04) needs the F11 tabular model and the F13 harness.

| Check | Test | Result |
|---|---|---|
| Platt shrinks overconfident raw probabilities; validation log loss improves | `tests/test_calibration.py` | pass |
| Every method is a recorded trial; lowest validation log loss wins | `tests/test_calibration.py` | pass |
| Property: both methods are symmetric within `2e-9` and stay in `[0.01, 0.99]` | `tests/test_calibration.py` | pass |
| Property: both methods preserve the ranking | `tests/test_calibration.py` | pass |
| Isotonic pooling is symmetric | `tests/test_calibration.py` | pass |
| Deliberate leakage: a window that overlaps training data is rejected | `tests/test_calibration.py` | pass |
| Duplicate match, mixed artifact, out-of-window row and known label are rejected | `tests/test_calibration.py` | pass |
| Too few rows or late labels give `BLOCKED`; sparse rows are not used | `tests/test_calibration.py` | pass |
| Spread mapping, bundle mismatch and in-window prediction refusal | `tests/test_calibration.py` | pass |
| Calibrated output passes the F12 calibration gate and can give `BET` | `tests/test_pricing_model_input.py` | pass |
| A shrinking calibrator can remove the edge | `tests/test_pricing_model_input.py` | pass |
| Calibrator bootstrap is seeded, stored in the hash and widens the spread | `tests/test_calibration.py` | pass |
| Tour calibrators need the row minimums; other tours use the pooled calibrator | `tests/test_calibration.py` | pass |

## Calibrator bootstrap (synthetic, seed 20261001)

- 400 overconfident rows (true slope 0.5), Platt only, 60 draws over 13 weeks, 0 failed.
- Fitted slope 0.416251. The draw slopes range from 0.181522 to 0.588874.
- A raw 0.80 with a base spread `[0.74, 0.85]` calibrates to 0.640387. The spread is
  `[0.607159, 0.673054]` without the calibrator bootstrap and `[0.577911, 0.711887]`
  with it (level 0.8).

## Tour calibrators (synthetic)

900 rows, tours assigned by row index, default minimums (50 fit, 30 validation rows):

| Segment | Method | Rows | Slope |
|---|---|---:|---:|
| pooled | isotonic | 900 | - |
| atp | isotonic | 648 | - |
| wta | Platt | 216 | 0.491509 |
| challenger | skipped: 24 fit and 12 validation rows, `BLOCKED` | 36 | - |

## Not demonstrated

- Calibration on real historical data and reliability diagrams.
- Tour calibrators and the calibrator bootstrap on real data.
- The gradient-boosting model, stacker and nested tuning (F11.1 to F11.4).
- Promotion against F13 gates.
