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

## Limitations

- Snapshots and datasets are stored in memory and as immutable files. The PostgreSQL
  tables exist in `0005_features`, but no repository writes them yet.
- F05 quotes and F06 policies do not have as-of readers yet. Quote features wait for F05.
- `AsOfView` scans all matches. This is acceptable for fixtures, not for a full warehouse.

See [SYS-07 evidence](evidence/SYS-07.md).
