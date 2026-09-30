# ADR 0005 — F15 scheduler

Date: 2026-09-30. Status: implemented for local and CI use. Supersedes the Prefect choice
in [ADR 0002](0002-f02-platform-foundation.md).

## Context

ADR 0002 selected Prefect for job orchestration and deferred the install until a
consumer exists. F15.1 and F15.2 now own the job graph, the dependency and publication
gates, idempotent job runs and fenced leases (`tennis_engine.operations.jobs`,
`tennis_engine.operations.leases`). The scheduler only has to start these jobs on a
cadence. It must not duplicate retries, run state or exactly-once logic.

## Evaluation of Prefect

Measured on 2026-09-30 with `uv add "prefect>=3,<4"` on a copy of the locked project:

| Measure | Locked stack | With Prefect 3.8.7 |
|---|---|---|
| Runtime packages (`uv export --no-dev`) | 37 | 115 (+78) |
| Runtime `site-packages` size | 203 MB | 306 MB (+103 MB) |

The resolution succeeded, so Prefect is compatible with the locked versions. It is still
too heavy for this use:

- Scheduled runs need a Prefect API server and its own database. That is one more
  service, one more store to back up and one more network listener.
- Prefect keeps its own run state, retries and caching. The F15 runner already owns these,
  so two systems would disagree about the state of a run.
- The new packages include `cloudpickle` (arbitrary object deserialization), a Docker
  client, a Redis client and an analytics client. F15.5 forbids arbitrary deserialization
  and needs scoped outbound access.

## Decision

Use a small scheduler in `tennis_engine.operations.scheduler`, run by
`tennis-ops scheduler`. It adds no dependency and no image size. The Compose service
`scheduler` replaces the former `worker` skeleton.

- One tick walks the job graph in topological order. Each job with a handler runs through
  `JobRunner`. Its cutoff is the start of its window (`every`), so a job runs once per
  window. A duplicate tick, a restart or a second scheduler finds the same run key and
  gets `ALREADY_SUCCEEDED`.
- A job without a handler is `INCOMPLETE`. Its dependants are `BLOCKED`, so publication
  cannot run on incomplete inputs. Only `sync_source_registry` has a handler now; it reads
  the F01 register and changes nothing.
- Operations tasks (alert evaluation, and later backups) run once per window under a lease
  `task:<name>`. They must be idempotent.
- The process serves `/metrics`, `/health` and an Alertmanager webhook on an internal port.
- Leases fence two schedulers, but one scheduler per host is the supported setup.

## Consequences

- The scheduler has no user interface. Prometheus and the logs show its state.
- Adding a job means writing a handler and a test. The dependency graph does not change.
- Revisit this decision if the project needs distributed workers or a run history UI.
