"""Shared OPS-01 drill fixtures for the in-memory and PostgreSQL job stores (synthetic)."""

from datetime import UTC, datetime, timedelta

import pytest

from tennis_engine.common.clock import FrozenClock
from tennis_engine.operations.jobs import (
    DEPENDENCIES,
    Capacity,
    CapacityLimits,
    CapacityPools,
    DependencyStatus,
    JobName,
    JobRunner,
)
from tennis_engine.operations.leases import LeaseLost

START = datetime(2026, 9, 20, 8, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 20, 7, tzinfo=UTC)
COMPLETE = DependencyStatus.COMPLETE
ALL_INPUTS = {name: COMPLETE for name in ("identity", "format", "policy", "quote")}


def complete(job):
    return {dependency: COMPLETE for dependency in DEPENDENCIES[job]}


def runner(store, leases, clock, owner="worker-a", ttl=timedelta(seconds=30), limits=None):
    return JobRunner(store, leases, CapacityPools(limits or CapacityLimits()), clock, owner, ttl)


def run_publish(job_runner, work, **overrides):
    values = {
        "resource": "job:publish_recommendations:shadow",
        "cutoff": CUTOFF,
        "input_versions": {"decisions": "synthetic-v1"},
        "dependencies": complete(JobName.PUBLISH_RECOMMENDATIONS),
        "work": work,
        "publication_inputs": ALL_INPUTS,
    }
    return job_runner.run(JobName.PUBLISH_RECOMMENDATIONS, **(values | overrides))


def drills(store_factory):
    """OPS-01 drills. `store_factory()` returns (job_store, lease_store) on a clean state."""
    clock = FrozenClock(START)
    effects: list[str] = []

    def work(lease):
        effects.append(f"publish:{lease.fencing_token}")
        return {"publication": f"batch-{lease.fencing_token}"}

    # Duplicate delivery and scheduler restart.
    store, leases = store_factory()
    first = run_publish(runner(store, leases, clock), work)
    assert first.status == "SUCCEEDED" and effects == ["publish:1"]
    duplicate = run_publish(runner(store, leases, clock, owner="worker-b"), work)
    assert duplicate.status == "ALREADY_SUCCEEDED" and effects == ["publish:1"]
    assert duplicate.run.job_run_id == first.run.job_run_id

    # Incomplete identity, format, policy or quote inputs cannot reach publication.
    blocked = run_publish(
        runner(store, leases, clock),
        work,
        input_versions={"decisions": "synthetic-v2"},
        publication_inputs=ALL_INPUTS | {"identity": DependencyStatus.INCOMPLETE},
    )
    assert blocked.status == "BLOCKED" and "INPUT:identity:INCOMPLETE" in blocked.detail
    missing = run_publish(
        runner(store, leases, clock),
        work,
        input_versions={"decisions": "synthetic-v3"},
        dependencies={},
    )
    assert "DEPENDENCY:evaluate_quotes:INCOMPLETE" in missing.detail
    assert effects == ["publish:1"]

    # Worker crash: the failure is recorded, the lease is freed, a retry succeeds.
    def crash(lease):
        raise RuntimeError("synthetic worker crash")

    crashed = run_publish(
        runner(store, leases, clock), crash, input_versions={"decisions": "synthetic-v4"}
    )
    assert crashed.status == "FAILED"
    retried = run_publish(
        runner(store, leases, clock), work, input_versions={"decisions": "synthetic-v4"}
    )
    assert retried.status == "SUCCEEDED"
    history = [a.status for a in store.attempts(retried.run.job_run_id)]
    assert history == ["RUNNING", "FAILED", "RUNNING", "SUCCEEDED"]

    # Bounded retries: after max_attempts failures the run is exhausted.
    for _ in range(2):
        run_publish(
            runner(store, leases, clock),
            crash,
            input_versions={"decisions": "synthetic-v5"},
            max_attempts=2,
        )
    exhausted = run_publish(
        runner(store, leases, clock), work, input_versions={"decisions": "synthetic-v5"}
    )
    assert exhausted.status == "EXHAUSTED"

    # Stale lock: worker A stalls past its lease; worker B takes over with a newer token.
    stale_effects: list[str] = []

    def stalled(lease):
        clock.advance(timedelta(seconds=31))
        taken = run_publish(
            runner(store, leases, clock, owner="worker-b"),
            lambda newer: stale_effects.append(f"b:{newer.fencing_token}") or {"batch": "b"},
            input_versions={"decisions": "synthetic-v6"},
        )
        assert taken.status == "SUCCEEDED", taken.detail
        stale_effects.append(f"a:{lease.fencing_token}")
        return {"batch": "a"}

    stale = run_publish(
        runner(store, leases, clock), stalled, input_versions={"decisions": "synthetic-v6"}
    )
    # A cannot record success after it lost the lease; B's success is the only one.
    assert stale.status == "FAILED" and "LEASE_LOST" in stale.detail
    statuses = [a.status for a in store.attempts(stale.run.job_run_id)]
    assert statuses.count("SUCCEEDED") == 1
    succeeded = [a for a in store.attempts(stale.run.job_run_id) if a.status == "SUCCEEDED"]
    assert succeeded[0].lease_owner == "worker-b"
    assert succeeded[0].fencing_token > int(stale_effects[-1].split(":")[1])

    # A fenced effect checks the token: the old lease cannot write.
    old = leases.acquire("source:synthetic-book:events", "worker-a", timedelta(seconds=5), START)
    clock.advance(timedelta(seconds=10))
    newer = leases.acquire(
        "source:synthetic-book:events", "worker-b", timedelta(seconds=5), clock.now()
    )
    assert old is not None and newer is not None
    assert newer.fencing_token > old.fencing_token
    with pytest.raises(LeaseLost):
        leases.renew(old, timedelta(seconds=5), clock.now())
    assert (
        leases.acquire(
            "source:synthetic-book:events", "worker-a", timedelta(seconds=5), clock.now()
        )
        is None
    )
    return store


def capacity_drill(store, leases):
    """Backfill capacity is separate from prospective collection."""
    clock = FrozenClock(START)
    limits = CapacityLimits(prospective=1, backfill=1)
    pools = CapacityPools(limits)
    job_runner = JobRunner(store, leases, pools, clock, "worker-a")
    with pools.slot(Capacity.BACKFILL) as taken:
        assert taken
        backfill = job_runner.run(
            JobName.SYNC_RESULTS,
            resource="job:sync_results:backfill-2025",
            cutoff=CUTOFF,
            input_versions={"range": "2025"},
            dependencies=complete(JobName.SYNC_RESULTS),
            work=lambda lease: {"rows": "0"},
            capacity=Capacity.BACKFILL,
        )
        assert backfill.status == "BUSY" and backfill.detail == "CAPACITY_FULL"
        prospective = job_runner.run(
            JobName.SYNC_RESULTS,
            resource="job:sync_results:today",
            cutoff=CUTOFF,
            input_versions={"range": "today"},
            dependencies=complete(JobName.SYNC_RESULTS),
            work=lambda lease: {"rows": "3"},
        )
        assert prospective.status == "SUCCEEDED"
