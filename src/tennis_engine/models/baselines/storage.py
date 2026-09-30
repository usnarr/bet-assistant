"""Immutable files for baseline artifacts and model cards (F09.7).

This is storage, not a promotion registry. Promotion and the model registry belong to
F11/F13; a stored baseline is only a shadow candidate or a rollback target.
"""

import os
from pathlib import Path

from tennis_engine.features.contracts import canonical_json
from tennis_engine.infrastructure.artifacts import ArtifactKind, ArtifactStore, create_manifest

from .contracts import BaselineArtifact, ModelCard


def _write_once(target: Path, content: bytes) -> None:
    if target.exists():
        if target.read_bytes() != content:
            raise FileExistsError(f"{target.name} is immutable")
        return
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def write_baseline(root: Path, artifact: BaselineArtifact, card: ModelCard) -> Path:
    if card.artifact_sha256 != artifact.artifact_sha256:
        raise ValueError("The model card describes a different artifact")
    body = canonical_json(artifact.model_dump(mode="json"))
    manifest = create_manifest(
        artifact_id=artifact.model_id,
        kind=ArtifactKind.MODEL,
        name=artifact.name,
        created_at=artifact.training_cutoff,
        content=body,
        code_revision=card.code_revision,
    )
    directory = ArtifactStore(root).write(manifest).parent
    _write_once(directory / "artifact.json", body)
    _write_once(directory / "model-card.json", canonical_json(card.model_dump(mode="json")))
    return directory


def read_baseline(directory: Path) -> tuple[BaselineArtifact, ModelCard]:
    artifact = BaselineArtifact.model_validate_json((directory / "artifact.json").read_bytes())
    card = ModelCard.model_validate_json((directory / "model-card.json").read_bytes())
    return artifact, card
