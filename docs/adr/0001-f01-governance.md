# ADR 0001 — F01 governance before external integration

Date: 2026-09-19. Status: implemented for local development.

The workspace initially contained design documents only. F01 is the first feature
in the catalog; implementing it does not imply implementation of the broader first
four iterations or approval to collect from any external source.

| Decision | Implementation | Outstanding release gate |
|---|---|---|
| Initial market/format | Internal shadow, ATP/WTA singles, best-of-three, pre-match match winner | Coverage validation in later features |
| First sports provider | Self-authored synthetic fixtures for development; external provider unselected | Product/data engineering select and approve a licensed provider using the comparison |
| First bookmaker | None enabled; independent pending records for Betclic, Superbet and Fortuna | Source-specific access review and rule evidence |
| Promotions | Disabled by default; future activation requires an explicit reviewed rule version | F06 payout and eligibility implementation |
| Policy owners | Product/policy reviewer for source access and payouts; product/risk reviewer for responsible use; operations may stop recommendations | Named people and independent review remain unassigned |
| Quote confirmation | Manual confirmation is mandatory in the later release; no wagering API | F14/F16 enforce quote freshness and confirmation in the release flow |
| Persistence | Local SQLite append-only journal and immutable evidence bytes; exact decimals in JSON | F02 migrates the contracts to PostgreSQL/Alembic and the private deployment |
| Policy time | Half-open effective intervals; each update is an immutable schedule snapshot with a server-recorded knowledge time | Consumers preserve revision IDs and use historical cutoffs |
| Rollback | Append a reviewed new revision; never overwrite journal/history or restore an old database over active revocations | Operations backup/recovery drill in F15 |
| Authorization | Trusted host injects the principal; local CLI uses an OS-account mapping protected by filesystem permissions | F14/F15 supply authenticated remote roles; agents get scoped read wrappers |

Only the package metadata, dependency lock and tooling needed for F01 are scaffolded.
This deliberately does not claim that F02 infrastructure, API or CI is complete.
SQLite gives this feature a durable, testable contract without requiring unrelated
services. Its SQL migration ships in the package; a future incompatible schema is
rejected. Destructive downgrade would erase audit evidence, so recovery is forward
only or from a verified backup into a separate database for investigation.

The local CLI is an administrative utility, not a security boundary against its
own OS account. A caller with filesystem/SQL access can change the role mapping or
database. Do not expose the store, role constructor, access-file parameter or CLI
to agent tools or dashboard users. Future server wrappers must obtain roles from
authentication and restrict database credentials; request bodies cannot set roles.

Responsible-use numerical values in tests are synthetic. Committed operational
configuration is pending review, globally/account disabled, with zero limits.
The governance gate never calculates an actionable stake. F12 must apply these
limits to transactional ledger state, including count, exposure and drawdown checks.
F03 must implement rate limiting from each approved quota. F06 must resolve actual
reviewed payout/settlement rules. A governance allow is not a `BET` decision.
