# MOD-04 evidence: F11 tabular model and stacker on synthetic data

Date: 2026-09-30. Status: engineering tests pass. All data is synthetic and fictional, so
none of these results is evidence that a model works on real matches. No promotion.

Command: `uv run pytest tests/test_tabular.py -q` (7 tests).

## Setup

`tests/backtest_support.py`, seed 11, 150 matches, cutoffs 24 hours and 1 hour before the
start. Three expanding outer folds of 12 days from 2026-02-09, 48 test rows each. Library
`xgboost-cpu` 3.4.1, seed 20261002, one thread.

## Nested tuning (training period of the last fold: 236 rows, 118 matches)

| Depth | Rate | Rounds | Monotone | Inner log loss | Brier | Slope |
|---:|---:|---:|---|---:|---:|---:|
| 2 | 0.1 | 100 | no | 0.565808 | 0.182459 | 0.673966 |
| 3 | 0.1 | 100 | no | 0.581990 | 0.186393 | 0.594721 |
| 2 | 0.05 | 250 | no | 0.577506 | 0.184970 | 0.623869 |
| 4 | 0.05 | 200 | no | 0.555660 | 0.179576 | 0.757113 |
| 2 | 0.1 | 100 | yes | 0.571644 | 0.184026 | 0.657856 |
| 3 | 0.1 | 100 | yes | 0.600166 | 0.192936 | 0.557472 |
| 2 | 0.05 | 250 | yes | 0.579326 | 0.184257 | 0.610756 |
| 4 | 0.05 | 200 | yes | 0.552113 | 0.178979 | 0.773467 |

Selected: depth 4, rate 0.05, 200 rounds, monotone. Every slope is below 1, so the raw
output is overconfident on inner folds; it needs an F11 calibrator before F12 use.

## Walk-forward results (144 test rows per model)

The harness run uses a two-entry grid (`tabular-xgb-small`) to keep the test short. The
paired difference is `model - surface Elo` mean log loss, with a 90% week-block interval
(200 draws, seed 20261001).

| Model | Log loss | Brier | Cal. slope | Difference | Interval |
|---|---:|---:|---:|---:|---|
| baseline-surface-elo | 0.535904 | 0.171139 | 0.929939 | - | - |
| baseline-ranking | 0.468489 | 0.151038 | 2.089128 | -0.067415 | [-0.103429, -0.022364] |
| tabular-xgb-small | 0.510993 | 0.157097 | 0.960824 | -0.024911 | [-0.072855, 0.026696] |
| tabular-xgb-small-market | 0.505672 | 0.155746 | 0.975865 | -0.030233 | [-0.079289, 0.026807] |
| stacked-ensemble | 0.499673 | 0.159482 | 1.218901 | -0.036232 | [-0.063554, 0.001080] |

The stack combines surface Elo, ranking and the small tabular model.

F09.8 (with and without market input), matched rows: `market - no market` log loss is
-0.005321, interval [-0.010163, -0.000401]. The synthetic prices are not derived from the
players' strengths, and nothing is quoted at the 24-hour cutoff. This difference only shows
that the comparison runs; it is not a finding about market information.

## Other checks

- Inner folds keep all cutoffs of a match in one fold. Inner training rows have earlier
  cutoffs and earlier labels, and never include a validation match.
- The schema is swap-closed and flags the missing market input.
- A refit gives an equal artifact. A swap gives `p + p_swap = 1` within `1e-12`.
- A monotone model is non-decreasing in `diff.elo`.
- A grid above the search budget, too few matches, a late row and an empty training set
  are rejected. Tampered booster bytes are refused.
- The stacker rejects an in-sample component, a component fitted after the row's cutoff
  and a row after the training cutoff. The stack is swap-symmetric.
- Every outer fit of every model is `FITTED`, and every stack prediction is scored.

## Not demonstrated

- Results on approved real data, and the MOD-03/MOD-04 comparison against market
  consensus on real quotes.
- A spread for the tabular output without a calibrator bootstrap.
- Signed artifacts, a promotion registry and rollback verification.
