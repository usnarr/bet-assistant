# SYS-04 evidence — identity resolution (synthetic)

Status: engineering tests pass on synthetic fixtures. Independent label review: pending.
Result classification: `INCONCLUSIVE` for release, because no approved real data exists.

Fixture: `tests/fixtures/identity/synthetic-warehouse-v1/` (fictional people and events).
Labels: `labels.json`, 9 player cases and 6 event cases, written before the tests ran.

| Category | Cases | Expected | Result |
|---|---|---|---|
| Reversed name order and diacritics with birth date | ID-001, ID-004 | `AUTO_ACCEPT` to labelled player | pass |
| Identical surnames | ID-002, EV-003 | `REVIEW_REQUIRED` | pass |
| Initials with birth date | ID-003 | `REVIEW_REQUIRED` (0.993 < 0.995) | pass |
| Same name, different birth date | ID-005 | `REVIEW_REQUIRED`, no merge | pass |
| Exact name only | ID-006 | `REVIEW_REQUIRED` | pass |
| Reused source ID with conflicting attributes | ID-007 | `REVIEW_REQUIRED` | pass |
| Stable source alias | ID-008 | `AUTO_ACCEPT` | pass |
| Wrong tour | ID-009 | `REVIEW_REQUIRED` | pass |
| Bookmaker names with scheduled opponent | EV-001, EV-002 | `AUTO_ACCEPT`, opposite `swapped` | pass |
| Outside schedule window, doubles, unknown player | EV-004 to EV-006 | `REVIEW_REQUIRED` | pass |

Known false merges in the label set: 0 of 3 accepted player cases.

Other behavior covered by `tests/test_identity_warehouse.py`:

- Qualifying and main-draw matches for the same player, and a walkover without a score.
- A result correction appends version 2 and keeps version 1 with its observation time.
- A rescheduled match keeps its identity; an earlier cutoff sees the earlier start time.
- Reprocessing the same batch writes nothing new; a checkpoint resumes a partial run.
- A missing stat count stays `None`; one player without stats counts as `missing_stats`.
- An invalid score is not written and opens a `RECORD` review.
- An unresolved participant blocks the match.
- Agent or system actors cannot approve a review; a remap lists matches to revalidate.

Command: `uv run pytest tests/test_identity_contracts.py tests/test_identity_resolution.py tests/test_identity_warehouse.py -q`.
