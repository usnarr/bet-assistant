# MOD-02 evidence — tennis formats, solver and simulator (synthetic)

Status: engineering tests pass. Independent review of the fixtures: pending.
Comparison with surface Elo: synthetic run through the F13 harness only
([F13 evidence](../../backtesting/evidence/F13.md)); held-out run on approved data: not run.
Release classification: `INCONCLUSIVE`; the point model stays `EXPERIMENTAL`.

Fixture: `tests/fixtures/tennis/mod-02-cases.json`, 45 cases. Expectations come from
`tests/tennis_oracle.py`, a plain recursive solver that shares no code with the engine.

| Kind | Cases | Covers |
|---|---:|---|
| `hold` | 11 | endpoints 0 and 1, advantage and no-ad deuce |
| `tiebreak_server` | 10 | A, B, B, A service order |
| `tiebreak` | 7 | 7- and 10-point targets, both first servers, win by two, one unbreakable server |
| `next_server` | 6 | rotation after even, odd and tiebreak sets |
| `match` | 8 | correct-score distributions for three best-of-three formats, both servers |
| `set` | 3 | set-score distributions including 7-6/6-7 |

Other checks in `tests/test_point_model.py`:

- Mass sums to one; only 2-0, 2-1, 1-2, 0-2 have mass; total games span 12 to 39.
- Equal players with an averaged first server give 0.5; swapping players and first server
  gives complementary probabilities.
- Exact and simulated win probability agree within the preregistered bound of 4 Monte
  Carlo standard errors (20 000 matches); the same seed reproduces, another seed differs.
- Unverified formats, best-of-five and advantage final sets are rejected.
- The point model uses only stats known at the training cutoff, is deterministic, recovers
  the ordering of the strongest and weakest synthetic servers, and abstains for an
  unverified format or sparse players.
- The fit converges on a 180-match history in fewer than 50 sweeps. The F13 harness found
  that plain coordinate steps did not converge within 500 sweeps above about 110 matches,
  because the likelihood is flat along the mean/effect shift. Each sweep now takes the
  exact prior-only step along those two directions.

Command: `uv run pytest tests/test_point_model.py -q`.
