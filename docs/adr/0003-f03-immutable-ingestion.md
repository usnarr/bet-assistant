# ADR 0003 — F03 immutable ingestion and replay

Date: 2026-09-19. Status: implemented for synthetic/local and isolated-service use.

F03 separates four facts that must not be conflated: an ingestion command, every fetch
attempt, immutable response content, and each successful source observation. PostgreSQL is
the durable system of record for metadata and lineage; it is not a cache. S3-compatible
storage contains deterministic gzip objects addressed by source and uncompressed SHA-256.
Repeated successful responses share content bytes but create distinct observations, while
failed attempts and replay-cache reads never refresh observation freshness.

The archive flow stages `raw_content`, writes the object, reads it back, verifies the
uncompressed hash, and only then marks it `ARCHIVED`. No cross-store transaction is
assumed. State changes have append-only transition history, and reconciliation recovers a
verified staged object or marks missing/corrupt bytes failed. Untracked objects are recorded
as orphans rather than silently deleted.

Fetchers recheck the current F01 source policy at execution time. HTTP retries are bounded
to configured transient cases, concurrency and request windows are capped per source,
credential-bearing URL fields are redacted, conditional requests are supported, and
`401`/`403`/CAPTCHA responses stop access. `429` records the bounded `Retry-After`
suspension. Access stops and rate-limit suspensions can be persisted as runtime events so a
new worker does not erase them. Re-enabling a stopped source is deliberately not automatic.

Parsers return strict validated records behind immutable version identifiers. Derived rows
are idempotent by source, record type, natural key, parser version, and canonical payload
hash. Parse failures append dead-letter evidence. Replay selects immutable observations by
source, resource/event and half-open time interval, records a job, supports dry-run, and
does not rewrite older parser outputs.

Redis is not introduced: future locks and cache views must be optional and rebuildable.
A vector database is also not part of F03. If F18 later needs semantic retrieval, its index
must be derived from approved, cutoff-valid evidence and cannot become authoritative for
identity, probabilities, payout, risk, or money decisions.

The included sports payload is explicitly synthetic. No live source is enabled, and no
production collection is permitted until a source policy and evidence pass F01 review.
