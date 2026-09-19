# SYS-01 — F01 implementation evidence

Executed 2026-09-19 on Windows with Python 3.13.15 and the committed `uv.lock`.

| Check | Observed result |
|---|---|
| `pytest -q` | **77 passed**, no skips |
| `ruff check .` | PASS |
| `ruff format --check .` | PASS, 12 Python files |
| `mypy src` | PASS, 7 source files |
| `uv sync --frozen --offline` | PASS |
| `uv build --offline` | Wheel and source distribution built; SQL migration included |
| CLI initialize/check-source smoke | Global disable on; unregistered source denied with `NO_BET` |
| Synthetic demonstration | **PASS**: queued callback never runs after revocation, publication denied, expired raw bytes removed with tombstone |
| External source/policy approvals | **BLOCKED**: no real source or payout policy approved |
| Independent fixture review | **BLOCKED**: no named independent reviewer assigned |

The overall release gate remains **BLOCKED**. Passing engineering tests does not
certify bookmaker rules, commercial data rights, production authentication,
financial limits or betting performance.

The suite covers source lifecycle/purpose/expiry, global and account stops,
cooling-off, unknown and changed evidence, newly discovered communications,
immutable versions, stale concurrent writers, reopened-store revocation,
effective-vs-known-time lookups, overlapping/adjacent policies, exact decimal and
timezone validation, Warsaw DST/calendar resets, forbidden agent/viewer mutations,
source-specific retention, licensed deletion, deletion rollback and replay loss.
CLI/configuration tests verify disabled draft entries for all source categories
and separate pending payout records for all three bookmakers.

Evidence bundle:

- [Machine-readable demonstration](SYS-01-demo.json).
- [Register and audit export](register-export.json); approvals within this export
  are explicitly **synthetic** and apply only to `synthetic-*` entities.
- [JUnit results](SYS-01-junit.xml).
- [Artifact and implementation hashes](manifest.json).
- Synthetic archived bytes: [terms](synthetic-terms.txt), [rules](synthetic-rules.txt),
  [limits](synthetic-limits.txt). The export contains their SHA-256 references.
- [Fixture provenance and pending independent review](../../../tests/fixtures/governance/manifest.json).
- [Provider comparison](../provider-comparison.md), [bookmaker review records](../bookmaker-reviews.md),
  and [architecture decisions](../../adr/0001-f01-governance.md).

Reproduce with the commands in the [operator guide](../README.md). Demo output is
written to a unique `var/f01-demo/<run-id>/` directory, preserving earlier runs.
The checked-in JSON report is evidence from one run, not a live status endpoint.

F01.3 remains open for actual reviewed document archives. F02 must replace the
local storage/authorization boundary for deployment; F03 must enforce collection
quotas and object-store retention, F06 supplies executable payout semantics, and
F12 enforces usage limits transactionally. None of those features is claimed here.
