# ADR 0004 — F04 canonical identity and versioned sports facts

Date: 2026-09-29. Status: implemented for synthetic fixtures and the in-memory store.

Identity uses internal UUIDs and versioned source aliases. The resolver scores evidence
with `1 - prod(1 - weight)` and returns `AUTO_ACCEPT`, `REVIEW_REQUIRED` or `REJECT`.
It never writes. The warehouse writes only accepted results; every other outcome opens a
review item. This keeps the proposal path (usable by F05 and F18 agents) separate from the
write path, which requires a human reviewer for any manual decision.

The policy validator rejects weights that let name evidence alone reach the review
threshold. The resolver also caps name-only scores. Two guards exist because a policy file
can change later, but the safety rule must not.

Deterministic UUIDs (`stable_id`) make reprocessing idempotent. A canonical match orders
its players by UUID text. This removes source player order from identity and from feature
direction. The alias stores `swapped` so adapters can orient quotes.

Facts that change over time (schedule, status, result, stats, rankings) are append-only
versions, each with an `Availability` record. The observation time comes from F03. We do
not derive it from event dates, so a historical import stays research-only until an
archive review proves earlier availability. Corrections are new versions, and labels for
evaluation can use the latest version while features use the version known at a cutoff.

Rejected alternatives: fuzzy string similarity scores as merge evidence (it hides the
reason for a merge); mutating an alias row on remap (it breaks as-of reads); inferring
best-of format from tournament level (the blueprint forbids a default format).
