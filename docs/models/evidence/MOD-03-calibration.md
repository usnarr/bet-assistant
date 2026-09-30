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

## Not demonstrated

- Calibration on real historical data, reliability diagrams and per-tour calibrators.
- Calibrator fit uncertainty in the spread.
- The gradient-boosting model, stacker and nested tuning (F11.1 to F11.4).
- Promotion against F13 gates.
