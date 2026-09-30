# F13 walk-forward evaluation and promotion guide

Package: `src/tennis_engine/backtesting/`. Status: the P0 baseline harness is implemented
on synthetic data. The P1 execution replay (F13.2 to F13.5) is implemented on
synthetic quote history, rules and results. No real quote history exists yet.

| Module | Purpose |
|---|---|
| `contracts.py` | `GateStatus`, `combine`, frozen `EvaluationConfig`, `ScoredPrediction` |
| `splits.py` | Expanding walk-forward folds grouped by match; split and fit validation |
| `runner.py` | Refit each candidate per fold and score the frozen snapshots (F13.1) |
| `metrics.py` | Log loss, Brier, calibration, accuracy, AUC, segments, matched pairs |
| `bootstrap.py` | Paired tournament-week block bootstrap (F13.8) |
| `economics.py` | Profit, ROI, drawdown, losing streak, CLV and coverage of settled bets |
| `market.py` | Market consensus as a harness candidate from F05 quote history |
| `replay.py` | Execution replay with F05 quotes, F12 decisions and F06 settlement |
| `promotion.py` | Machine-readable release decision with every gate (F13.9) |
| `bundle.py` | Immutable run directory with hashed files and a manifest |
| `studies.py` | Feature-set ablation on matched cutoffs (F08.8) |

## Splits

- `walk_forward(snapshots, boundaries)` makes fold `k` for `[b_k, b_k+1)`. Its training
  cutoff is `b_k`, and the training window expands.
- A match belongs to the fold of its earliest cutoff. Every cutoff and orientation of that
  match stays in that fold. Rows before the first boundary are warm-up training only.
  Rows after the last boundary are excluded and counted.
- `validate_split` rejects a match that appears in two folds.
- `validate_fit` rejects a training row that:
  - belongs to the tested fold or a later fold;
  - has a snapshot cutoff after the training cutoff; or
  - has a label observed after the training cutoff.
- The runner calls `validate_fit` for every fold.

## Runner

- Candidates:
  - `BaselineCandidate`: an F09 ranking, global Elo or surface Elo baseline.
  - `PointCandidate`: the F10 point model. It fits on serve counts known at the training
    cutoff and needs a verified match format for each match.
- Training labels come from `label_known_at(training_cutoff)`. Only completed, retired and
  defaulted results are used. Walkovers and missing results are not scored.
- Evaluation labels come from the final corrected result. `ScoredPrediction` rejects a
  label observed at or before the prediction cutoff.
- A fit that raises `ValueError` is recorded as `BLOCKED`. Its fold rows are
  `UNSUPPORTED` with reason `fit_blocked`.
- `RunResult.content_sha256` covers the split, every fit and every prediction, so a rerun
  proves reproducibility.

## Metrics and uncertainty

- Log loss is the primary metric, then Brier and calibration intercept/slope. Accuracy and
  AUC are secondary.
- Every segment shows its rows, supported rows, scorable rows and coverage. Segments are
  `all`, each fold, and each value of the configured tags (tour, surface, cutoff).
- A segment below `min_segment_rows` is `INCONCLUSIVE`.
- Model comparisons use only rows that both models score, with the same snapshot and label.
  They are reported next to each model's full coverage.
- Block bootstrap intervals resample whole ISO weeks of the snapshot cutoff, with a fixed
  seed and draw count. With fewer than two blocks, the interval is missing.

## Economics

`summarize` takes settled virtual bets and the decision list.

- Replay order: equity follows settlement order from the starting bankroll.
- Drawdown and losing streak: drawdown is measured from the running peak. A void neither
  extends nor resets a losing streak.
- CLV: this is `odds / closing_odds - 1` and exists only with a comparable closing quote and
  a named comparability policy. Missing closing data is counted as missing, not as zero.
- Assumptions: a replay that is not execution-grade must name its assumptions.

## Market consensus candidate

`ConsensusCandidate(history_quotes(history, keys))` in `market.py` runs the F09 consensus
in the harness. `keys(match_id)` lists the F05 quote keys of a match.

- At each snapshot cutoff, each key gives its latest observation by the cutoff, mapped with
  the latest event mapping known by the cutoff. A key without a mapping gives nothing.
- The candidate has no fitted parameters. Its artifact hash is the hash of its
  `ConsensusConfig`.
- Pass `consensus="market-consensus"` to `decide_promotion`. The
  `consensus_calibration_noninferior` gate then compares calibration on matched rows.
  Without a consensus model in the run, the gate stays `BLOCKED`.

## Execution replay

`replay(cases, services, scenario, run_id=..., opening_balance=...)` in `replay.py` runs
the production F05, F06 and F12 code on recorded history (F13.2 to F13.5).

- **State at the cutoff (F13.2):** each `ReplayCase` is one decision. The replay reads
  only what was known at the cutoff: the latest event mapping, the quote observations and
  their actionability, the settlement rule, payout policy, publication and responsible-use
  decisions in effect, and the exposure of the replay ledger. It then runs F12 `decide`.
  A model output generated after the cutoff is rejected.
- **Latency (F13.3):** a BET is executed at the first quote observed at or after
  `cutoff + latency_seconds`. The replay runs `decide` again at that time with the same
  policy. A price, risk or capacity change resizes or cancels the bet. This later quote
  evaluates execution only; the prediction and its features stay those of the cutoff.
  `prepare_publication` then rechecks the volatile gates and reserves the stake.
- **Accepted-stake caps (F13.4):** an `AcceptedStakeCap` observed by the execution time
  becomes the bookmaker maximum in `StakeRules`, so F12 sizes within it and recomputes the
  payout. A cap of zero is an observed rejection. The latest observation wins; a
  match-specific cap wins a tie.
- **Scenarios (F13.4):** `ExecutionScenario` sets the latency and these stress settings:
  an odds haircut, a deterministic suspension rate, a capacity fraction, and an assumed cap
  for bets without an observed cap. Each stress setting is an assumption.
- **Settlement (F13.5):** F06 `settle` settles each struck bet against every result
  version, at the time that version was observed. A different final result becomes a
  ledger correction. A later pending version does not undo a final result. Results are
  labels only; they never change a prediction input.
- **CLV:** the closing price is the last observation of the same quote key before the
  scheduled start, if it is open (`same-quote-key-last-open-before-start-v1`).
- **Execution grade:** a run is execution-grade only when it has no assumptions. These
  make a named assumption: reconstructed quote history, a research-only case, a struck bet
  without an observed cap, and any stress setting. `ReplayRun.summary(...)` passes the
  grade and the assumptions to `summarize`, so `net_economics_positive` stays `BLOCKED`
  for a run that is not execution-grade.
- **Ledger:** each run uses a fresh virtual ledger. `ReplayRun.reconciliation` is the F06
  reconciliation at the end. Nothing places a bet.

## Promotion decision

`decide_promotion` records these gates, and the worst status is the decision:

| Gate | PASS needs |
|---|---|
| `config_frozen_and_complete` | every required setting set, and `frozen_at` before evaluation |
| `sample_sufficient` | `min_test_rows` matched rows in `min_blocks` weeks |
| `beats_surface_elo_log_loss` | upper bound of `candidate - surface Elo` log loss below zero |
| `consensus_calibration_noninferior` | calibration deviation within the margin of market consensus |
| `no_severe_calibration_defect` | every evaluable segment inside the slope and intercept limits |
| `stable_across_folds` | the candidate wins at least `min_winning_fold_fraction` of evaluable folds |
| `no_single_week_dependency` | the improvement stays after the best week is removed |
| `net_economics_positive` | execution-grade replay with a net ROI lower bound above the minimum |
| `no_leakage` | the leakage suite passed |
| `reproducible_run` | a rerun has the same content hash |
| `rollback_artifact` | a rollback target is named |
| `independent_reviewer_signoff` | an approving reviewer who is not the author |

- Missing evidence is `BLOCKED`.
- An unproven improvement is `INCONCLUSIVE`.
- A worse candidate, a self-review or a configuration changed after evaluation is `FAIL`.
- A `PASS` is not deployment. The audited champion switch stays outside this code.

`configs/evaluations/release.json` is a draft. Every threshold is unset, so every promotion
is `BLOCKED` until the thresholds are frozen on development data.

## Bundle

`write_bundle(root, run, ...)` writes `<root>/<run_id>/` with these files:
`predictions.jsonl`, `fits.jsonl`, `metrics.json`, `failures.jsonl`, `report.md`,
`release-decision.json` and `manifest.json`.

- The manifest holds the SHA-256 of every other file, the configuration and the caller's
  provenance.
- An existing directory is never overwritten.
- Predictions use JSON Lines, not Parquet, because the project has no Parquet dependency.
- Store only relative names in the bundle. Never write a local path.

## Not implemented

- An execution-grade replay on real data. It needs approved sources, recorded quote
  history, observed stake caps and reviewed rules. Until then, `net_economics_positive`
  is `BLOCKED` for every real candidate.
- Segments by tournament level, odds bucket, bookmaker, market and data quality. They need
  those tags on the snapshots or quotes.
- Opening, 6-hour and 15-minute cutoffs. `pre_match_cutoffs` accepts any offset, but no
  data supports these cutoffs yet.
- A CLI entry point.

See [F13 evidence](evidence/F13.md).
