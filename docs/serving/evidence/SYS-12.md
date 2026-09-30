# SYS-12 evidence — F14 API, dashboard and permissions

Date: 2026-09-30. Scope: synthetic decision records from the F12 `decide()` fixtures,
synthetic match context, synthetic tokens and an isolated governance journal. No real
player, quote, source approval or redistribution right exists.

## Local run

| Check | Result |
|---|---|
| `uv sync --frozen` | pass |
| `uv run ruff format --check .` | pass (185 files) |
| `uv run ruff check .` | pass |
| `uv run mypy src` | pass (124 source files) |
| `uv run pytest -q` with isolated PostgreSQL 17.6 and SeaweedFS 4.47 | 564 passed, 0 skipped |
| F14 unit tests (`test_serving_api.py`, `test_serving_explain.py`, `test_serving_dashboard.py`) | 64 passed |
| `tests/integration/test_serving_persistence.py` | 1 passed |
| `uv run python scripts/check_migrations.py` | pass (single head `0010_decision_records`) |
| `uv run pip-audit` | no known vulnerabilities; no dependency added |

The integration tests run `alembic downgrade base`, `upgrade head` and `downgrade base`
against the isolated database. CI on `main` passed for the API and store commits.

## SYS-12 fixture coverage

| Case | Test | Result |
|---|---|---|
| BET, WATCH and NO_BET views; exact decimal strings | `test_every_decision_is_listed_with_exact_decimal_strings` | pass |
| WATCH and NO_BET have a zero recommended stake and list reasons | same | pass |
| No money, odds or probability field is a JSON number | `test_no_decimal_money_field_is_a_json_number` | pass |
| Unavailable metrics are null, not zero | `test_unavailable_metrics_are_null_not_zero` | pass |
| Pagination is complete, stable and bound to its filters | `test_pagination_is_deterministic_and_complete` | pass |
| 11 invalid filters return explicit error codes | `test_invalid_filters_have_explicit_errors` | pass |
| Bookmaker, decision, market and start-time filters | `test_filters_select_bookmaker_decision_market_and_start_time` | pass |
| Expired decision leaves the current view, stays in history and audit | `test_expired_decision_leaves_the_current_list_but_stays_in_history_and_audit` | pass |
| History is never actionable | `test_history_view_is_never_actionable` | pass |
| Disabled source, suspended market, missing quote state, expired quote, global disable | `test_read_time_hard_stop_makes_a_cached_bet_not_actionable` (5 cases) | pass |
| Kill switch, global disable and ended payout policy on the real F01 store | `test_serving_explain.py` governance tests | pass |
| Failed read check fails closed | `test_failed_read_check_fails_closed` | pass |
| Every current read rechecks (no cache) | `test_every_current_read_rechecks_and_nothing_is_cached` | pass |
| Superseded version hidden from current view; audit keeps the chain | `test_superseded_version_is_hidden_from_current_but_kept_in_audit` | pass |
| Missing match, missing recommendation and malformed ID | `test_missing_entities_have_explicit_errors` | pass |
| Missing evidence shown as MISSING | `test_analysis_compares_players_and_lists_rejected_reasons` | pass |
| Permission denial: viewer and agent roles cannot read audit | `test_viewer_roles_cannot_read_audit` | pass |
| No write route exists; other methods return 405 | `test_the_api_has_no_write_route` | pass |
| Token required on every route; query-string tokens rejected | `test_every_route_requires_a_valid_token` | pass |
| Redistribution: viewers do not see withheld source values | `test_viewer_does_not_see_values_from_sources_without_redistribution_rights` | pass |
| Redistribution needs an approved F01 purpose | `test_governance_redistribution_needs_an_approved_redistribution_purpose` | pass |
| Responses are not cacheable | `test_responses_are_not_cacheable` | pass |
| PostgreSQL store equals the in-memory store on every query; append-only | `tests/integration/test_serving_persistence.py` | pass |

## Explanation comparison (F14.5)

| Check | Test | Result |
|---|---|---|
| Every number in an explanation is a stored value (BET, WATCH, NO_BET) | `test_every_number_in_an_explanation_is_a_stored_value` | pass |
| No motivation, injury, certainty or profit words | same | pass |
| Observed, inferred, missing and decision sentences are separate | `test_explanation_separates_observed_inferred_missing_and_decision` | pass |
| A BET says to confirm the quote manually and that no bet is placed | `test_bet_explanation_requires_manual_confirmation` | pass |
| Withheld sources hide values | `test_withheld_sources_hide_values_in_explanations` | pass |

The banned-word check is a fixed list. It is not a proof that no unsupported claim can
occur. The templates contain no free text from a language model.

## Dashboard (F14.3, F14.4)

| Check | Test | Result |
|---|---|---|
| All decisions shown with the same badge and plain reasons | `test_list_shows_every_decision_with_reasons_and_exact_numbers` | pass |
| Displayed numbers follow the documented format; exact value in `title` | same | pass |
| Blocked BET labelled "recorded BET; blocked at read time" | `test_blocked_bet_is_labelled_as_blocked_at_read_time` | pass |
| No script, only a GET form, CSP without script sources, `lang`, skip link, captions | `test_pages_are_read_only_accessible_and_script_free` | pass |
| Audit link only for internal roles; audit page denied to viewers | match and audit page tests | pass |
| Canonical text is HTML-escaped | `test_canonical_text_is_escaped` | pass |

## Latency sample

Setup: in-process `TestClient`, in-memory store with 200 synthetic records on one match,
fake read checks, one Windows development machine, 300 sequential requests after 10
warm-up requests.

| View | Median | p95 | Max |
|---|---|---|---|
| `GET /v1/tennis/recommendations?limit=50` | 8.4 ms | 11.5 ms | 20.3 ms |
| `GET /v1/tennis/matches/{id}/analysis` | 42.6 ms | 79.5 ms | 101.7 ms |
| `GET /dashboard?limit=50` | 9.0 ms | 13.7 ms | 57.4 ms |

This sample excludes network, PostgreSQL and the governance journal. It is not the
staging measurement that the plan asks for.

## Not demonstrated

- A browser review and an assistive-technology review of the dashboard.
- The F14.6 explanation agent (AG-EX and AG-X).
- Production wiring of stores, credentials and the governance journal.
- Any approved redistribution right, or a legal review of derived numbers for viewers.
- Behavior on real data, real quotes or real bookmaker rules.
- A p95 measurement at a declared staging workload.
