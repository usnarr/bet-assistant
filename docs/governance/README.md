# F01 operator guide

The three independent policy families are in `configs/governance/`: source access,
bookmaker/PL payout rule references, and responsible use. All supplied external
entries are drafts or pending review. Initial global disable is **on**, including
when no configuration exists. Failed lookups return typed reasons and `NO_BET`.

## Local setup and audited updates

Run `uv sync --frozen`, then `uv run tennis-governance init`. The SQLite schema is
created idempotently. To perform administrative writes, the local workspace owner
creates `var/governance-access.json` with the intended OS account and role:

```json
{"YOUR_OS_ACCOUNT": "policy_reviewer"}
```

No reviewer is preconfigured. Protect that file and database with OS permissions.
An absent/unlisted account is read-only (`dashboard`); an unknown role is rejected.
This is local development authorization, not production authentication. See
[ADR 0001](../adr/0001-f01-governance.md) before integrating a server or agent.

Apply a draft register entry:

```powershell
uv run tennis-governance apply source configs/governance/sources/betclic-odds.json --expected-revision 0 --reason "Initialize disabled draft"
uv run tennis-governance apply payout configs/governance/payouts/betclic.json --expected-revision 0 --reason "Record pending independent review"
uv run tennis-governance apply responsible_use configs/governance/responsible_use/internal-shadow.json --expected-revision 0 --reason "Initialize disabled shadow limits"
uv run tennis-governance export
```

Use the current entity's journal revision for every update, not a global counter.
Stale writes fail. Keep a new schedule's still-applicable intervals explicitly;
omitted intervals become unavailable at the new knowledge time. Overlapping
intervals and reused version IDs with different content fail validation.

Archive locally supplied **permitted** document bytes before approving a policy:

```powershell
uv run tennis-governance archive-document betclic-regulations payout:betclic var/reviewed-regulations.pdf --reference "Official URL or contract reference" --reason "Received for review"
```

The command returns the exact content hash; archival alone is pending review.
Each approval needs a reviewer matching the local authenticated principal, a
review date no earlier than evidence archival, a future review deadline, effective
interval, and the complete evidence set. Sources additionally require approved
purpose, access method, quotas, retention and explicit rights for production or
redistribution. Approval dates do not rewrite when evidence became known.

Source states: `DRAFT`, `PROTOTYPE_APPROVED`, `PRODUCTION_APPROVED`, `SUSPENDED`.
Only a reviewer can change them. Prototype approval allows only prototype use.
Revocation is a new source version with `kill_switch: true` or `SUSPENDED`; the next
execution-time lookup rejects it. Production is a separate explicit approval.

```powershell
uv run tennis-governance check-source betclic-odds production
uv run tennis-governance global-disable on --reason "Incident under investigation"
```

An operator may turn the global stop on. Only a reviewer can turn it off. Turning
it off does not override source, payout, account, review expiry or cooling-off
checks. Agent and ordinary dashboard roles cannot alter policies or evidence.

## Consumer contracts

- `can_fetch(source_id, purpose, now)` returns a decision with source version/revision.
  `execute_fetch(...)` checks immediately before invoking the queued fetch callback.
- `get_payout_policy(bookmaker, effective_at, known_at)` distinguishes applicable
  dates from evidence and policy knowledge. Missing/review-required is `NO_BET`.
- `get_responsible_use_policy(account_scope, now)` returns only an approved,
  currently enabled, non-cooling-off policy, subject to the global stop.
- `publication_gate(...)` rechecks all source dependencies and both other policy
  families at publication time. It grants only governance permission. F06/F12
  must still enforce payout support, stake/count/exposure/drawdown and all other
  recommendation gates. Never cache an allow result across execution/publication.

Storage failures raise exceptions and must stop the calling job/publication;
there is no fallback to permissive defaults. F15 should count reason codes,
pending document reviews, expired reviews and overdue deletion jobs in monitoring.

## Responsible-use limits and resets

Policies define daily/weekly/monthly stake **and count** limits, event/open
exposure, maximum bankroll fraction, drawdown stop, cooling-off and account
disable. Scope includes account, virtual/actual ledger and PLN currency.
Period resets use Warsaw calendar midnight; weeks start Monday. Spring and autumn
days can be 23 or 25 hours. Monetary values use exact decimals and serialize as
strings. Loss chasing cannot be enabled. Operational seed values are zero and
disabled; fixture amounts are not product defaults. F12 owns usage accounting and
transactional reservations; these contracts alone do not enforce a bankroll.

## Retention and recovery

`RetentionService` is a local F01 adapter for the future F03 raw store. Each object
records its source version, hash, ingestion-based expiry and retention basis.
Duplicate immutable objects are idempotent; changed bytes cannot overwrite them.
Expired objects immediately become unavailable for replay, even before cleanup.
`uv run tennis-governance expire-raw` removes due bytes and appends tombstones in
the same transaction. Early licensed deletion needs a reviewer and mandate
reference. Retried deletion is idempotent and deleted IDs cannot be resurrected.

This is logical deletion from the active database, not a guarantee of secure
erasure from disk pages, backups or replicas. A real licensed source must have
backup/physical-erasure requirements implemented and reviewed in F02/F03/F15
before this local adapter is used for its data. Policy evidence has a separate
audit retention need; do not archive a document here unless that retention is
permitted. The implementation does not run a scheduler or collect external data.

Back up the database with SQLite's backup API while running, or copy it only when
closed. Inspect restored copies separately. Do not roll back a live governance
database past a revocation. Repair through a reviewed new version. Deletion fault
tests verify a failed byte removal leaves no misleading tombstone.

## Validation and evidence

Run `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, and
`uv run mypy src`. The tests use temporary databases and synthetic content only.
Run `uv run python scripts/demo_governance.py` for a reproducible denied-fetch and
publication demonstration plus register export in `var/f01-demo/`.

See [SYS-01 evidence](evidence/SYS-01.md), [provider comparison](provider-comparison.md)
and [independent bookmaker records](bookmaker-reviews.md). Engineering fixture
success does not replace the remaining named human approvals.
