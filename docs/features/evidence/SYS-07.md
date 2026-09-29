# SYS-07 evidence — leakage matrix (synthetic)

Status: engineering tests pass on a synthetic miniature history (`tests/pit_support.py`).
Independent review: pending. Release classification: `INCONCLUSIVE` (no approved data).

| Mutation from the evaluation plan | Test | Result |
|---|---|---|
| Target final score added after the cutoff | `test_target_result_after_cutoff_does_not_change_the_snapshot` | snapshot hash unchanged; a post-result cutoff raises `LeakageError` |
| Ranking published after the cutoff, dated earlier | `test_ranking_published_after_cutoff_with_earlier_date_is_rejected` | rejected, hash unchanged |
| Old data imported today, no evidence | `test_old_import_without_evidence_is_research_only` | invisible in `PROSPECTIVE`; `RESEARCH_ONLY` class in research mode |
| Import with reviewed archive evidence | `test_archived_evidence_is_accepted_only_in_archived_mode` | `ARCHIVED` class only in archived mode |
| Result corrected after the cutoff | `test_result_correction_after_cutoff_keeps_the_known_version` | version 1 used; label uses version 2 |
| Match rescheduled after the cutoff | `test_reschedule_after_cutoff_uses_schedule_known_at_cutoff` | schedule known at the cutoff; cutoff not moved |
| Identity corrected after the cutoff | `test_corrected_alias_after_cutoff_is_invisible_to_the_view` | earlier alias version returned |
| Realized weather or late forecast | `test_forecast_rules_reject_realized_and_late_weather` | rejected |
| Future rows added | `test_future_rows_leave_earlier_snapshots_unchanged` | snapshot equal |
| Frozen dataset rebuilt | `test_frozen_dataset_rebuild_is_identical_and_immutable` | identical manifest, rows and artifact |

Negative controls (a legitimately available input must change the feature):

- A ranking observed before the cutoff changes `rank`.
- A past match observed before the cutoff changes `history_matches` and the hash.

Split, preprocessing, stacker and calibration mutations from the matrix belong to F13/F11
(PLAN 4). Closing-price and quote-interval mutations wait for F05 quotes.

The F07 fixture found one F04 defect: two matches with the same pairing from one source
were merged. Commit `c008d22` fixed it and added a regression test.

Command: `uv run pytest tests/test_point_in_time.py -q`.
