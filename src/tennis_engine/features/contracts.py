"""F07 contracts: availability modes, input lineage, snapshots and dataset manifests."""

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp, VersionRef
from tennis_engine.contracts.domain import AvailabilityClass, FeatureValue, FeatureVector

_RANK = {
    AvailabilityClass.PROSPECTIVE: 0,
    AvailabilityClass.ARCHIVED: 1,
    AvailabilityClass.RESEARCH_ONLY: 2,
}


class AvailabilityMode(StrEnum):
    """Which proof of availability a query boundary accepts.

    ``PROSPECTIVE`` accepts only our own observations by the cutoff. ``ARCHIVED`` also
    accepts reviewed archive evidence. ``RESEARCH_ONLY`` also accepts facts whose effective
    time precedes the cutoff; results in this mode can never support execution claims.
    """

    PROSPECTIVE = "PROSPECTIVE"
    ARCHIVED = "ARCHIVED"
    RESEARCH_ONLY = "RESEARCH_ONLY"


def worst(classes: "list[AvailabilityClass] | tuple[AvailabilityClass, ...]") -> AvailabilityClass:
    """The weakest proof among inputs decides the class of the output."""
    if not classes:
        return AvailabilityClass.PROSPECTIVE
    return max(classes, key=_RANK.__getitem__)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class InputRef(Contract):
    """One fact version used by a feature, with how its availability was proven."""

    kind: Identifier
    key: Annotated[str, Field(min_length=1, max_length=512)]
    version: Annotated[int, Field(ge=1, strict=True)]
    availability: AvailabilityClass
    observed_at: Timestamp
    source_id: Identifier


class FeatureDefinition(Contract):
    """Versioned documentation that travels with every snapshot (F08.1)."""

    name: Identifier
    unit: Annotated[str, Field(min_length=1, max_length=64)]
    direction: Literal["PLAYER_ONE_MINUS_TWO", "PLAYER_ONE", "PLAYER_TWO", "MATCH"]
    description: Annotated[str, Field(min_length=1, max_length=512)]
    window: Annotated[str, Field(max_length=64)] = ""
    source_requirement: Annotated[str, Field(max_length=256)] = ""
    missing_behavior: Annotated[str, Field(min_length=1, max_length=256)]


class FeatureSnapshot(Contract):
    """Immutable wide row keyed by match, cutoff and feature-set version (F07.2)."""

    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    as_of: Timestamp
    feature_set: Identifier
    feature_set_sha256: Digest
    mode: AvailabilityMode
    availability: AvailabilityClass
    player_ids: tuple[UUID, UUID]
    values: dict[Identifier, FeatureValue]
    missing: tuple[Identifier, ...]
    inputs: tuple[InputRef, ...]
    snapshot_sha256: Digest

    @model_validator(mode="after")
    def consistent(self) -> Self:
        for name in self.missing:
            if self.values.get(name) is not None:
                raise ValueError(f"{name} is marked missing but has a value")
        for value in self.values.values():
            if isinstance(value, Decimal) and not value.is_finite():
                raise ValueError("Feature values must be finite")
        expected = snapshot_digest(
            self.match_id,
            self.as_of.isoformat(),
            self.feature_set,
            self.feature_set_sha256,
            self.mode,
            self.values,
            self.missing,
            self.inputs,
        )
        if expected != self.snapshot_sha256:
            raise ValueError("Snapshot hash does not match its content")
        if self.availability != worst([item.availability for item in self.inputs]):
            raise ValueError("Snapshot class must be the weakest input class")
        return self

    @property
    def research_only(self) -> bool:
        return self.availability == AvailabilityClass.RESEARCH_ONLY

    def to_vector(self) -> FeatureVector:
        return FeatureVector(
            match_id=self.match_id,
            as_of=self.as_of,
            feature_set=VersionRef(
                component="feature-set", version=self.feature_set, sha256=self.feature_set_sha256
            ),
            values=self.values,
            input_hashes=(self.snapshot_sha256,),
        )


def snapshot_digest(
    match_id: UUID,
    as_of: str,
    feature_set: str,
    feature_set_sha256: str,
    mode: AvailabilityMode,
    values: dict[str, FeatureValue],
    missing: tuple[str, ...],
    inputs: tuple[InputRef, ...],
) -> str:
    return digest(
        {
            "match_id": str(match_id),
            "as_of": as_of,
            "feature_set": feature_set,
            "feature_set_sha256": feature_set_sha256,
            "mode": mode.value,
            "values": {key: _encode(value) for key, value in sorted(values.items())},
            "missing": list(missing),
            "inputs": [item.model_dump(mode="json") for item in inputs],
        }
    )


def _encode(value: FeatureValue) -> object:
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    return value


class ReproducibilityInfo(Contract):
    code_revision: Annotated[str, Field(min_length=7, max_length=64, pattern=r"^[0-9a-f]+$")]
    tzdata_version: Annotated[str, Field(min_length=1)]
    feature_config_sha256: Digest
    source_versions: dict[Identifier, Identifier]
    random_seed: int | None = None


class DatasetManifest(Contract):
    """Immutable description of a frozen dataset. Mixed classes are never averaged away."""

    schema_version: Literal["1.0"] = "1.0"
    dataset_id: UUID
    name: Identifier
    created_at: Timestamp
    mode: AvailabilityMode
    availability: AvailabilityClass
    research_only: bool
    feature_set: Identifier
    feature_set_sha256: Digest
    cutoff_rule: Annotated[str, Field(min_length=1, max_length=256)]
    rows: tuple[tuple[UUID, Timestamp, Digest], ...]
    excluded: dict[str, int] = Field(default_factory=dict)
    class_counts: dict[AvailabilityClass, int]
    reproducibility: ReproducibilityInfo
    content_sha256: Digest

    @model_validator(mode="after")
    def classification_is_honest(self) -> Self:
        if self.research_only != (self.availability == AvailabilityClass.RESEARCH_ONLY):
            raise ValueError("research_only must match the dataset availability class")
        present = [name for name, count in self.class_counts.items() if count > 0]
        if present and self.availability != worst(present):
            raise ValueError("Dataset class must be the weakest row class")
        if self.mode == AvailabilityMode.PROSPECTIVE and self.availability != (
            AvailabilityClass.PROSPECTIVE
        ):
            raise ValueError("A prospective dataset cannot contain archived or research rows")
        return self
