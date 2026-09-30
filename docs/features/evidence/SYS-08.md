# SYS-08 evidence — tennis feature formulas (synthetic)

Status: engineering tests pass. Independent review of the hand calculations: pending.
Release classification: `INCONCLUSIVE` (no approved data, no ablation study yet).

Hand-calculated fixtures in `tests/test_tennis_features.py`:

| Formula | Input | Expected | Result |
|---|---|---|---|
| Elo expectation | 1500 vs 1500 | 0.5 | pass |
| Elo update, K = 32 | winner and loser at 1500 | 1516 / 1484 | pass |
| Elo expectation | 1516 vs 1484 | 0.545922 | pass |
| Default K | 0 matches: `250 / 5 ** 0.4` | 131.326390 | pass |
| Inactivity shrinkage | 1600, 150 idle days, threshold 90, retention 0.97 | 1594.09 | pass |
| Form | one win, expected 0.5, age = half-life 60, prior weight 2 | form 0.1, ESS 1 | pass |
| Form ESS | weights 0.5 and 1 | 1.8 | pass |
| Beta-Binomial | 10/20, prior 0.6, n0 20 | 0.55 | pass |
| Shrunk serve rate in a snapshot | 56/80, prior 0.64, n0 200 | 0.657143 | pass |

Behavior checks:

- Player swap: the reversed orientation equals `swap_values` of the canonical row.
- New player: Elo prior with count 0, `form = None`, `stats_missing = True`, shrunk rate
  equals the prior, raw rate `None`. No NaN.
- Unknown surface: surface Elo and its difference are `None`; surface stays `UNKNOWN`.
- Walkovers never rate; retirements are excluded unless configured.
- Quality: unknown identity, odds or rules block; a high soft score cannot unblock;
  unknown best-of fails the format gate.
- Lineage: every input `observed_at` is at or before the cutoff; stats inputs appear.
- SYS-07 also checks that `core-v1` ignores the target result and later matches.

F08.6 (`tests/test_environment_features.py`): with no permitted source, weather is missing
and flagged; a permitted forecast issued before the cutoff is used; late, realized,
late-archived and unapproved forecasts are ignored and leave the snapshot unchanged;
indoor venues need no weather; Warsaw to New York in September gives a 6-hour proxy shift.

Not yet covered: F08.8 ablations through F13, and fitted priors.

Command: `uv run pytest tests/test_tennis_features.py -q`.
