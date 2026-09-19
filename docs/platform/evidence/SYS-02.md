# SYS-02 platform foundation evidence

Date: 2026-09-19. Scope: F02 repository, contracts, migrations, storage, API, worker,
and CI foundation.

## Local result

| Check | Result | Evidence |
|---|---|---|
| Locked environment | PASS | `uv lock --check` resolved the committed Python 3.13 lock |
| Format and lint | PASS | Ruff format check and lint completed with no findings |
| Static typing | PASS | Strict mypy completed with no findings across 25 source files |
| Unit/contract/integration tests | PASS | 93 passed against local PostgreSQL 17.6 and MinIO |
| Migration structure | PASS | One head at `0002_governance`; full PostgreSQL upgrade compiled offline |
| Compose smoke | PASS | API and worker started; `/health/ready` confirmed both dependencies |
| Package build | PASS | Source archive and wheel built as version 0.2.0 |
| Dependency audit | PASS | No known third-party vulnerabilities; local project was not queried on PyPI |
| Runtime image scan | PASS | Trivy 0.70.0 found zero fixed HIGH/CRITICAL issues |

The service integration cases used loopback-only PostgreSQL and MinIO containers. They
exercised migration upgrade/downgrade/re-upgrade, append-only mutation rejection, and an
immutable object round trip. The API reported database revision `0002_governance` and the
`tennis-raw` bucket ready. CI provisions the same dependency classes for every change.
Developers without Docker can still run the portable suite; in that mode only the two
explicit service cases skip and cannot be presented as full SYS-02 evidence.

The runtime image is built in a separate stage, contains only the locked production
environment, and removes unused `pip`/`ensurepip`. This removed vulnerable build-only
vendored packages from the runtime image; Trivy then reported zero HIGH or CRITICAL fixed
vulnerabilities across Debian and Python packages.

## Covered failure behavior

- Naive timestamps, binary floats, invalid money precision, duplicate match players, and
  inconsistent probability distributions fail contract validation.
- `WATCH` and `NO_BET` cannot carry a positive stake; `BET` cannot contain a failed gate.
- Local objects and artifact manifests reject different bytes under an existing identity.
- Production settings reject placeholder credentials and non-TLS object storage.
- API liveness stays independent of readiness; an unavailable dependency returns HTTP 503.
- Readiness rejects missing migrations and revisions other than `0002_governance`.
- PostgreSQL governance/audit tables use append-only triggers and start globally disabled.
- Structured logging and public configuration output redact secrets and URL query strings.

External sports/bookmaker access, model quality, payout correctness, and recommendation
release are outside F02 and remain disabled or unimplemented.
