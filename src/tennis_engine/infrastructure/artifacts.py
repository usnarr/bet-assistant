"""Content-addressed manifests for datasets, models, evaluations, and agent traces."""

import hashlib
import json
import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field

from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp, VersionRef


class ArtifactKind(StrEnum):
    DATASET = "dataset"
    MODEL = "model"
    EVALUATION = "evaluation"
    AGENT_TRACE = "agent_trace"


class ArtifactManifest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    artifact_id: UUID
    kind: ArtifactKind
    name: Identifier
    created_at: Timestamp
    content_sha256: Digest
    code_revision: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-f]+$")
    inputs: tuple[VersionRef, ...] = ()
    source_revisions: dict[Identifier, int] = Field(default_factory=dict)

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()

    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, manifest: ArtifactManifest) -> Path:
        directory = self.root / manifest.kind / str(manifest.artifact_id)
        target = directory / "manifest.json"
        content = manifest.canonical_bytes() + b"\n"
        if target.exists():
            if target.read_bytes() != content:
                raise FileExistsError("Artifact manifest is immutable")
            return target
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".manifest.{os.getpid()}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def read(self, kind: ArtifactKind, artifact_id: UUID) -> ArtifactManifest:
        target = self.root / kind / str(artifact_id) / "manifest.json"
        return ArtifactManifest.model_validate_json(target.read_bytes())


def create_manifest(
    *,
    artifact_id: UUID,
    kind: ArtifactKind,
    name: str,
    created_at: datetime,
    content: bytes,
    code_revision: str,
    inputs: tuple[VersionRef, ...] = (),
) -> ArtifactManifest:
    return ArtifactManifest(
        artifact_id=artifact_id,
        kind=kind,
        name=name,
        created_at=created_at,
        content_sha256=hashlib.sha256(content).hexdigest(),
        code_revision=code_revision,
        inputs=inputs,
    )
