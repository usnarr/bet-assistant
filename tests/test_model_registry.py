"""F11.8 model registry, F13.9 audited champion switch and rollback (synthetic bundles)."""

from datetime import UTC, datetime, timedelta

import pytest
from registry_support import (
    FAMILY,
    OPERATOR,
    REVIEWER,
    blocked_decision,
    bundle,
    drill_pass_decision,
)

from tennis_engine.backtesting.contracts import GateStatus
from tennis_engine.common.clock import FrozenClock
from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.models.registry import (
    NO_CHAMPION,
    EventKind,
    FileRef,
    InMemoryRegistryStore,
    ModelRegistry,
    RegistryRefused,
    StaleChampion,
    build_bundle,
    verify_bundle,
)

NOW = datetime(2026, 9, 30, 13, tzinfo=UTC)


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    root = tmp_path_factory.mktemp("artifacts")
    first = bundle(root, "v1", seed=1)
    second = bundle(root, "v2", seed=2, rollback_target=first.bundle_id)
    return root, first, second


@pytest.fixture
def registry(bundles, tmp_path):
    root, first, second = bundles
    instance = ModelRegistry(InMemoryRegistryStore(), root, FrozenClock(NOW))
    assert instance.register(first) is True
    assert instance.register(first) is False
    assert instance.register(second) is True
    return instance


def supported(*items):
    return frozenset(item.feature_set_sha256 for item in items)


def promote(registry, target, **overrides):
    decision = overrides.pop(
        "decision", drill_pass_decision(target.candidate, target.rollback_reference)
    )
    return registry.promote(
        target.bundle_id,
        decision,
        actor=overrides.pop("actor", REVIEWER),
        reason=overrides.pop("reason", "Synthetic drill switch"),
        supported_feature_sets=overrides.pop("supported", supported(target)),
        **overrides,
    )


def refused(call) -> tuple[str, ...]:
    with pytest.raises(RegistryRefused) as caught:
        call()
    return caught.value.reasons


def test_bundle_records_hashes_lineage_and_rollback_target(bundles):
    root, first, second = bundles
    assert first.bundle_id != second.bundle_id
    assert second.rollback_target == first.bundle_id
    assert first.rollback_reference == NO_CHAMPION
    assert second.rollback_reference == str(first.bundle_id)
    assert first.code_revision == "0123456789abcdef"
    assert first.dependency_lock_sha256 == "c" * 64
    assert first.calibrator is not None and first.calibrator_artifact_sha256 is not None
    assert set(first.files()) == {
        "model",
        "model_manifest",
        "model_card",
        "calibrator",
        "calibrator_manifest",
        "evaluation_report",
    }
    assert verify_bundle(root, first) == () and verify_bundle(root, second) == ()
    for ref in first.files().values():
        assert not ref.path.startswith("/") and ":" not in ref.path


def test_file_references_stay_inside_the_artifact_root(tmp_path):
    for bad in ("/etc/passwd", "../outside.json", "a\\b.json", "c:/x.json"):
        with pytest.raises(ValueError):
            FileRef(path=bad, sha256="a" * 64)


def test_a_mixed_bundle_is_refused_at_registration(bundles):
    root, first, second = bundles
    model_dir = first.model.resolve(root).parent
    other_calibrator = second.calibrator.resolve(root).parent
    reasons = refused(
        lambda: build_bundle(
            root,
            family=FAMILY,
            version="mixed",
            model_dir=model_dir,
            calibrator_dir=other_calibrator,
            evaluation_report=first.evaluation_report.resolve(root),
            rollback_target=None,
            registered_by="fixture",
            registered_at=NOW,
        )
    )
    assert "CALIBRATOR_BELONGS_TO_ANOTHER_MODEL" in reasons


def test_registration_needs_a_known_rollback_target_and_a_unique_version(bundles):
    root, first, second = bundles
    registry = ModelRegistry(InMemoryRegistryStore(), root, FrozenClock(NOW))
    assert "ROLLBACK_TARGET_UNKNOWN" in refused(lambda: registry.register(second))
    registry.register(first)
    duplicate = first.model_copy(update={"bundle_id": second.bundle_id, "rollback_target": None})
    with pytest.raises(RegistryRefused):
        registry.store.register(duplicate)


def test_the_real_synthetic_decision_is_blocked_and_cannot_promote(registry, bundles):
    _, first, _ = bundles
    decision = blocked_decision(first.rollback_reference)
    assert decision.candidate == first.candidate == "baseline-ranking"
    assert decision.status == GateStatus.BLOCKED
    reasons = refused(lambda: promote(registry, first, decision=decision))
    assert any(item.startswith("DECISION_NOT_PASS:") for item in reasons)
    assert registry.champion(FAMILY) is None
    assert registry.store.events(FAMILY) == ()


def test_switch_needs_a_reviewer_who_is_not_the_author(registry, bundles):
    _, first, _ = bundles
    ok = first.rollback_reference
    cases = {
        "ROLE_CANNOT_SWITCH": dict(actor=OPERATOR),
        "AUTHOR_CANNOT_SWITCH": dict(
            actor=Principal(identity="fixture-model-author", role=Role.POLICY_REVIEWER)
        ),
        "REVIEWER_IS_AUTHOR": dict(
            decision=drill_pass_decision(first.candidate, ok, reviewer="fixture-model-author")
        ),
        "NO_APPROVING_REVIEW": dict(
            decision=drill_pass_decision(first.candidate, ok, reviewer=None)
        ),
        "DECISION_FOR_ANOTHER_CANDIDATE": dict(decision=drill_pass_decision("other-model", ok)),
        "DECISION_NAMES_ANOTHER_ROLLBACK_TARGET": dict(
            decision=drill_pass_decision(first.candidate, "some-other-bundle")
        ),
        "REASON_REQUIRED": dict(reason="  "),
        "FEATURE_SET_NOT_SUPPORTED": dict(supported=frozenset()),
    }
    for expected, overrides in cases.items():
        assert expected in refused(lambda o=overrides: promote(registry, first, **o)), expected
    forged = drill_pass_decision(first.candidate, ok).model_copy(
        update={"content_sha256": "f" * 64}
    )
    assert "DECISION_ID_MISMATCH" in refused(lambda: promote(registry, first, decision=forged))
    assert registry.store.events(FAMILY) == ()


def test_switch_rollback_and_history_are_audited(registry, bundles):
    root, first, second = bundles
    both = supported(first, second)
    promoted = promote(registry, first)
    assert promoted.kind == EventKind.PROMOTE and promoted.previous_bundle_id is None
    assert registry.champion(FAMILY) == first
    # The rollback target of the second bundle must be registered and verify.
    event = promote(registry, second, supported=both)
    assert (event.sequence, event.previous_bundle_id, event.actor) == (
        2,
        first.bundle_id,
        REVIEWER.identity,
    )
    loaded = registry.active(FAMILY, both)
    assert loaded is not None and loaded.bundle == second
    assert loaded.calibrator.base_artifact_sha256 == loaded.artifact.artifact_sha256

    # A decision promotes once, and a champion cannot be promoted again.
    reused = drill_pass_decision(second.candidate, second.rollback_reference)
    assert "DECISION_ALREADY_USED" in refused(
        lambda: promote(registry, second, decision=reused, supported=both)
    )

    # An operator rolls back to the declared target: the complete earlier bundle.
    back = registry.rollback(FAMILY, actor=OPERATOR, reason="Drill", supported_feature_sets=both)
    assert back.kind == EventKind.ROLLBACK and back.bundle_id == first.bundle_id
    assert registry.active(FAMILY, both).bundle == first
    # Rolling back the first bundle goes to "no champion": scoring abstains.
    empty = registry.rollback(FAMILY, actor=OPERATOR, reason="Drill", supported_feature_sets=both)
    assert empty.bundle_id is None and registry.active(FAMILY, both) is None
    assert "NO_CHAMPION" in refused(
        lambda: registry.rollback(FAMILY, actor=OPERATOR, reason="x", supported_feature_sets=both)
    )
    history = registry.store.events(FAMILY)
    assert [item.kind for item in history] == ["PROMOTE", "PROMOTE", "ROLLBACK", "ROLLBACK"]
    assert all(item.reason and item.actor for item in history)


def test_rollback_refuses_unverified_or_unrelated_targets(registry, bundles, tmp_path):
    root, first, second = bundles
    both = supported(first, second)
    promote(registry, first)
    promote(registry, second, supported=both)
    viewer = Principal(identity="viewer", role=Role.DASHBOARD)
    assert "ROLE_CANNOT_ROLL_BACK" in refused(
        lambda: registry.rollback(FAMILY, actor=viewer, reason="x", supported_feature_sets=both)
    )
    stranger = second.model_copy(update={"bundle_id": FAMILY_ID})
    assert "ROLLBACK_TARGET_NOT_ALLOWED" in refused(
        lambda: registry.rollback(
            FAMILY,
            actor=OPERATOR,
            reason="x",
            supported_feature_sets=both,
            target=stranger.bundle_id,
        )
    )
    assert "ROLLBACK_TARGET:FEATURE_SET_NOT_SUPPORTED" in refused(
        lambda: registry.rollback(
            FAMILY, actor=OPERATOR, reason="x", supported_feature_sets=frozenset()
        )
    )
    assert registry.champion(FAMILY) == second


FAMILY_ID = __import__("uuid").UUID(int=77)


def test_a_changed_file_refuses_rollback_and_loading(bundles, tmp_path):
    import shutil

    source, first, second = bundles
    root = tmp_path / "copy"
    shutil.copytree(source, root)
    registry = ModelRegistry(InMemoryRegistryStore(), root, FrozenClock(NOW))
    registry.register(first)
    registry.register(second)
    both = supported(first, second)
    promote(registry, first)
    promote(registry, second, supported=both)
    calibrator = first.calibrator.resolve(root)
    calibrator.chmod(0o644)
    calibrator.write_bytes(calibrator.read_bytes().replace(b'"rows"', b'"rows" ', 1))
    reasons = refused(
        lambda: registry.rollback(FAMILY, actor=OPERATOR, reason="x", supported_feature_sets=both)
    )
    assert "ROLLBACK_TARGET:HASH_MISMATCH:calibrator" in reasons
    assert registry.champion(FAMILY) == second
    card = second.model_card.resolve(root)
    card.chmod(0o644)
    card.unlink()
    assert "FILE_MISSING:model_card" in refused(lambda: registry.active(FAMILY, both))


def test_concurrent_switch_is_detected(registry, bundles):
    _, first, second = bundles
    event = promote(registry, first)
    later = event.model_copy(update={"sequence": 2, "recorded_at": NOW + timedelta(seconds=1)})
    with pytest.raises(StaleChampion):
        registry.store.append(later, None)
