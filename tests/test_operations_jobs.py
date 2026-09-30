"""F15.1/F15.2 job graph, gates, leases and OPS-01 drills on the in-memory stores."""

from datetime import timedelta

import pytest
from operations_support import START, capacity_drill, drills

from tennis_engine.operations.jobs import (
    DEPENDENCIES,
    ORDER,
    DependencyStatus,
    InMemoryJobStore,
    JobName,
    dependency_blockers,
    publication_blockers,
    topological_order,
)
from tennis_engine.operations.leases import InMemoryLeaseStore


def memory():
    leases = InMemoryLeaseStore()
    return InMemoryJobStore(leases), leases


def test_graph_covers_every_blueprint_job_and_has_no_cycle():
    assert set(DEPENDENCIES) == set(JobName)
    position = {job: index for index, job in enumerate(ORDER)}
    for job, dependencies in DEPENDENCIES.items():
        assert all(position[dependency] < position[job] for dependency in dependencies)
    # Publication depends, through the graph, on identity, features, quotes and rules.
    upstream, pending = set(), [JobName.PUBLISH_RECOMMENDATIONS]
    while pending:
        for dependency in DEPENDENCIES[pending.pop()]:
            if dependency not in upstream:
                upstream.add(dependency)
                pending.append(dependency)
    assert {
        JobName.RESOLVE_ENTITIES,
        JobName.NORMALIZE_MARKETS,
        JobName.BUILD_FEATURES,
        JobName.SYNC_RULES_DOCUMENTS,
        JobName.RUN_DATA_QUALITY_CHECKS,
    } <= upstream
    cyclic = dict(DEPENDENCIES) | {JobName.SYNC_SOURCE_REGISTRY: frozenset({JobName.SYNC_RESULTS})}
    with pytest.raises(ValueError, match="cycle"):
        topological_order(cyclic)


def test_gates_treat_unknown_as_incomplete():
    assert publication_blockers({}) == (
        "INPUT:identity:INCOMPLETE",
        "INPUT:format:INCOMPLETE",
        "INPUT:policy:INCOMPLETE",
        "INPUT:quote:INCOMPLETE",
    )
    failed = {JobName.EVALUATE_QUOTES: DependencyStatus.FAILED}
    assert "DEPENDENCY:evaluate_quotes:FAILED" in dependency_blockers(
        JobName.PUBLISH_RECOMMENDATIONS, failed
    )
    assert dependency_blockers(JobName.SYNC_SOURCE_REGISTRY, {}) == ()


def test_ops01_drills_on_memory_stores():
    drills(memory)


def test_backfill_capacity_is_separate():
    capacity_drill(*memory())


def test_lease_ttl_is_bounded_and_release_keeps_the_token():
    leases = InMemoryLeaseStore()
    with pytest.raises(ValueError):
        leases.acquire("r", "a", timedelta(0), START)
    first = leases.acquire("r", "a", timedelta(seconds=5), START)
    assert first is not None
    leases.release(first)
    second = leases.acquire("r", "b", timedelta(seconds=5), START + timedelta(seconds=1))
    assert second is not None and second.fencing_token == first.fencing_token + 1
