"""Immutable calibrator files beside their base artifact (F11.8).

A scoring bundle is one base artifact plus one calibrator. ``read_bundle`` refuses a
calibrator that names another base artifact, so a rollback cannot mix versions.
"""

from pathlib import Path

from tennis_engine.features.contracts import canonical_json
from tennis_engine.infrastructure.artifacts import ArtifactKind, ArtifactStore, create_manifest
from tennis_engine.models.baselines.contracts import BaselineArtifact
from tennis_engine.models.baselines.storage import _write_once, read_baseline

from .contracts import CalibratorArtifact


def write_calibrator(root: Path, calibrator: CalibratorArtifact, *, code_revision: str) -> Path:
    body = canonical_json(calibrator.model_dump(mode="json"))
    manifest = create_manifest(
        artifact_id=calibrator.calibrator_id,
        kind=ArtifactKind.MODEL,
        name=calibrator.name,
        created_at=calibrator.window_end,
        content=body,
        code_revision=code_revision,
    )
    directory = ArtifactStore(root).write(manifest).parent
    _write_once(directory / "calibrator.json", body)
    return directory


def read_bundle(
    base_directory: Path, calibrator_directory: Path
) -> tuple[BaselineArtifact, CalibratorArtifact]:
    base, _ = read_baseline(base_directory)
    calibrator = CalibratorArtifact.model_validate_json(
        (calibrator_directory / "calibrator.json").read_bytes()
    )
    if (base.name, base.artifact_sha256) != (
        calibrator.base_model,
        calibrator.base_artifact_sha256,
    ):
        raise ValueError("The calibrator belongs to a different base artifact")
    return base, calibrator
