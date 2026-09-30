# F04 sports warehouse and identity guide

F04 turns validated source records into canonical players, tournaments, editions and
matches with internal UUIDs. It resolves source identities with evidence, sends uncertain
cases to a human review queue, and stores every changing fact as a new version.

## Rules that the code enforces

- Names only generate candidates. Name evidence alone stays below the review threshold,
  in the policy validator and again in the resolver.
- A hard attribute conflict (tour or birth date) excludes a candidate. A nationality
  difference does not, because nationality can change.
- `AUTO_ACCEPT` needs a stable source alias or corroboration such as an equal birth date
  or a scheduled match with the named opponent. Thresholds `0.995` and `0.90` are initial
  candidates to validate, not calibrated values.
- Name-only candidates block automatic creation. The record goes to review, so a possible
  duplicate is not created silently.
- Only sources listed in `ResolutionPolicy.allow_create` can create canonical players or
  tournaments. Bookmaker sources cannot.
- Only a human reviewer can approve, reject or remap. Actors that start with `system:` or
  `agent:` get `PermissionError`. The migration repeats this rule as a check constraint.
- A remap appends a new alias version and returns the matches to revalidate. Older alias
  versions stay available for as-of reads.
- Canonical matches store players in ascending UUID-text order. Source order is kept as
  `swapped` on the alias and on event resolutions.
- Schedule, status, result, stats and ranking facts are append-only versions. Each version
  has its own `Availability`: `observed_at` comes from the F03 observation, never from the
  event date. A result correction is a new version with `corrects_version`.
- A missing stat count stays `None`. It never becomes zero performance.
- Unknown best-of format and doubles are flagged, not guessed. Invalid scores, winners or
  states go to review as `RECORD` items.
- Deciding-set rules (`EditionFormatVersion`) are versions per edition, draw stage and
  best-of format. `ingest_format` appends a version only when the rule or its reference
  changes. A rule for an unresolved edition, or for an unknown stage or format, goes to
  review. `deciding_set_known_at` returns only a rule observed by the cutoff; a missing or
  `UNKNOWN` rule returns `None`, and the F10 point model then abstains.

## Main entry points

| Module | Purpose |
|---|---|
| `normalization/contracts.py` | Shared contracts, including the `IdentityResolver` protocol used by F05 |
| `normalization/resolver.py` | `EvidenceResolver`: `resolve_player` and `resolve_event`; no writes |
| `normalization/warehouse.py` | `SportsWarehouse`: ingest records, review decisions, remaps |
| `normalization/backfill.py` | Resumable backfill from F03 derived records and coverage reports |
| `normalization/store.py` | `IdentityStore` protocol and the in-memory reference store |
| `normalization/postgres.py` | `PostgresIdentityStore`, the same behavior on PostgreSQL |
| `normalization/formats.py` | Point-in-time deciding-set rule lookup |
| `migrations/versions/0004_identity.py` | PostgreSQL schema with append-only triggers |
| `migrations/versions/0009_identity_formats.py` | Format rule versions and the name-key index |

F05 adapters call `resolve_event(EventQuery, at=...)`. A result with
`blocks_recommendations == True` must stop every recommendation for that event.

## Backfill and reports

`Backfill.run` reads F03 derived records in a deterministic order (observation time, record
type, natural key). It saves a checkpoint after each batch, so an interrupted run resumes
from the last full batch. Reprocessing the same batch changes nothing. The sealed
`BackfillReport` lists accepted and review counts, per season/tour coverage, and how many
facts have verified archive evidence. Facts without that evidence are research-only for
historical replay (F07).

## Limitations

- `PostgresIdentityStore` takes an advisory lock per versioned key, so concurrent writers
  cannot skip or duplicate a version. Each method is its own transaction, as in the
  reference store; a warehouse operation that writes several rows is not one transaction.
  It passed against an isolated PostgreSQL database; it has no production run.
- Name candidates use the `player_name_key` index. Keys come from the canonical name and
  every alias name.
- Tournament identity is per source alias. Cross-source tournament and match linking needs
  a reviewed mapping step; it is not automatic.
- The resolver scans all matches for schedule context. This is acceptable for fixtures,
  not for a full historical warehouse.
- No real provider is approved. The SYS-04 fixtures are synthetic and fictional, and the
  independent label review is pending.
- The 99.5% supported-player resolution target cannot be measured without approved data.

See [ADR 0004](../adr/0004-f04-identity.md) and [SYS-04 evidence](evidence/SYS-04.md).
