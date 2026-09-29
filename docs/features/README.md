# F07 point-in-time data and F08 features guide

F07 makes sure every feature uses only information that the chosen mode can prove was
available at the prediction cutoff. F08 feature groups read only through this layer.

## Availability modes

| Mode | Accepts | Use |
|---|---|---|
| `PROSPECTIVE` | Our own observations with `observed_at <= as_of` | Shadow and live operation, execution-grade replay |
| `ARCHIVED` | Also reviewed archive evidence with `source_available_at <= as_of` | Historical replay with a verified evidence reference |
| `RESEARCH_ONLY` | Also facts whose `effective_at <= as_of` | Labelled research only; never an execution or profit claim |

Every read returns an `InputRef` with the class that proved it. A snapshot and a dataset
take the weakest class of their inputs. A `PROSPECTIVE` dataset cannot contain archived or
research rows; the manifest validator and the `0005_features` check constraints reject it.

## Entry points

| Module | Purpose |
|---|---|
| `features/asof.py` | `AsOfView`: schedule, status, result, stats, ranking, alias and completed-match reads at a cutoff; `forecast_usable` |
| `features/snapshots.py` | `build_features(store, match_id, as_of, feature_set, mode=...)`, `FeatureSet`, `SnapshotStore` |
| `features/dataset.py` | `pre_match_cutoffs`, `build_dataset`, `write_dataset` |
| `features/labels.py` | `final_label`: latest corrected result, for evaluation only |
| `features/context.py` | Core context and workload features (`core-context-v1`) |

## Rules

- The latest version *proven available* at the cutoff wins. Later corrections, remaps and
  reschedules are invisible to an earlier cutoff.
- A ranking counts only if its ranking date is on or before the cutoff and its observation
  (or archive evidence) is too. A late publication with an earlier date is rejected.
- History excludes the target match and every match that ended at or after the cutoff.
  Ties sort by end time, then match ID.
- `build_features` raises `LeakageError` if the target result, or a non-pre-match status,
  is already known at the cutoff.
- `pre_match_cutoffs` uses the first known scheduled start. A row is excluded, with a
  counted reason, if the schedule was not yet known at the cutoff.
- Weather: only forecasts issued by the cutoff and valid at the start time known at the
  cutoff. Realized weather is outcome analysis only.
- Labels come from `final_label` and never flow back into snapshots.
- Snapshots are content-addressed. Storing a different snapshot under an existing
  `(match, as_of, feature_set)` key fails. A changed feature needs a new feature-set version.
- Dataset manifests record code revision, `tzdata` version, feature-set hash, source
  versions, seed, row hashes and exclusion counts.

## F08 core feature set `core-v1`

`features/core.py` defines `CORE_SET` with four groups. Every feature has a
`FeatureDefinition` with unit, direction, window and missing behavior; the set hash
covers definitions and configuration, so any change needs a new version.

| Group | Features | Missing behavior |
|---|---|---|
| `context` | tour, surface, environment, best-of, round, draw stage, ranks, `diff.log_rank`, age, days since last match, matches/sets/minutes in 3/7/14/28 days, `duration_incomplete_*` | `None` plus missing flag; counts are real zeros; UNKNOWN stays UNKNOWN |
| `ratings` | global Elo, surface Elo, support counts, differences | prior rating with count 0; surface Elo `None` for UNKNOWN surface |
| `form` | shrunk half-life mean of `won - expected`, effective sample size, match count | `None` with no matches in the window |
| `serve_return` | Beta-Binomial shrunk and raw serve/return point-win rates, point counts, `stats_missing`, `stats_coverage` | raw rate `None` for 0 attempts; shrunk rate = prior with `stats_missing = True` |

Rules:

- Elo replays only matches that `AsOfView` proves completed before the cutoff, in its
  fixed order. Walkovers never update ratings. Retirements are excluded by default
  (`EloConfig.retirements`).
- Inactivity shrinkage pulls a rating toward the initial value by
  `inactivity_monthly_retention` for every 30 days beyond `inactivity_days`.
- Form weight is `0.5 ** (age_days / half_life_days)`; value is
  `sum(w * residual) / (sum(w) + prior_weight)`; ESS is `sum(w) ** 2 / sum(w ** 2)`.
- Shrunk rate is `(successes + prior_mean * n0) / (attempts + n0)`. Walkovers count
  neither as stats evidence nor as missing stats.
- `p1`/`p2` follow the canonical match order. `swap_values` gives the reversed orientation:
  it exchanges `p1`/`p2` and negates `diff.*`. The player-swap test checks this.
- All values are `Decimal` quantized to 6 places. No value can be NaN or infinite.
- All parameters (`EloConfig`, `FormConfig`, `ServeReturnConfig`) are candidate values.
  Fitting priors and scales on training periods only belongs to F09/F13.

## Quality gates (F08.7)

`features/quality.py` `assess(snapshot, identity_resolved=..., odds_ready=..., rules_ready=...)`
returns a `QualityReport`. Hard components are identity, timing, format, odds and rules.
A hard component that is `FAIL` or `UNKNOWN` sets `usable = False`. The soft score (stats
coverage) is informative only. Odds and rules stay `UNKNOWN` until F05/F06 provide them,
so the report blocks recommendations by default.

## Limitations

- Snapshots and datasets are stored in memory and as immutable files. The PostgreSQL
  tables exist in `0006_features`, but no repository writes them yet.
- F05 quotes and F06 policies do not have as-of readers yet. Quote features wait for F05.
- `AsOfView` scans all matches, and each snapshot replays Elo from the start. This is
  acceptable for fixtures, not for a full warehouse; an incremental rating cache is needed.
- F08.6 travel/weather features and F08.8 ablations are not implemented yet.

See [SYS-07 evidence](evidence/SYS-07.md) and [SYS-08 evidence](evidence/SYS-08.md).
