from datetime import UTC, datetime

import pytest

from tennis_engine.common.ids import stable_id
from tennis_engine.infrastructure.artifacts import ArtifactKind, ArtifactStore, create_manifest
from tennis_engine.infrastructure.object_store import LocalObjectStore


def test_local_object_store_is_immutable_and_content_addressed(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    metadata = store.put("sports/sample.json", b"fixture")
    assert metadata.sha256 == "f16d05ec6b29248d2c61adb1e9263f78e4f7bace1b955014a2d17872cfe4064d"
    assert store.get(metadata.key) == b"fixture"
    assert store.put(metadata.key, b"fixture") == metadata
    with pytest.raises(FileExistsError, match="Immutable"):
        store.put(metadata.key, b"changed")
    with pytest.raises(ValueError, match="relative POSIX"):
        store.put("../escape", b"bad")


def test_artifact_manifest_is_stable_and_immutable(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = create_manifest(
        artifact_id=stable_id("artifact", "dataset-one"),
        kind=ArtifactKind.DATASET,
        name="baseline-dataset",
        created_at=datetime(2026, 9, 19, 10, tzinfo=UTC),
        content=b"dataset",
        code_revision="abcdef1",
    )
    target = store.write(manifest)
    assert store.write(manifest) == target
    assert store.read(ArtifactKind.DATASET, manifest.artifact_id) == manifest
    changed = manifest.model_copy(update={"code_revision": "1234567"})
    with pytest.raises(FileExistsError, match="immutable"):
        store.write(changed)
