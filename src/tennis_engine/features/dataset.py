"""Frozen point-in-time datasets and reproducibility manifests (F07.2, F07.6).

A dataset records its availability mode and the weakest class of any row. Rows that
cannot be built safely are excluded with a counted reason; they are never filled.
"""

import os
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import UUID

from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import AvailabilityClass
from tennis_engine.infrastructure.artifacts import (
    ArtifactKind,
    ArtifactManifest,
    ArtifactStore,
    create_manifest,
)
from tennis_engine.normalization.store import IdentityStore

from .asof import AsOfView
from .contracts import (
    AvailabilityMode,
    DatasetManifest,
    FeatureSnapshot,
    ReproducibilityInfo,
    canonical_json,
    digest,
    worst,
)
from .snapshots import FeatureSet, LeakageError, build_features


def tzdata_version() -> str:
    try:
        return version("tzdata")
    except PackageNotFoundError:
        return "system"


def pre_match_cutoffs(
    store: IdentityStore,
    match_ids: Iterable[UUID],
    *,
    offset: timedelta,
    mode: AvailabilityMode,
) -> tuple[list[tuple[UUID, datetime]], Counter[str]]:
    """Cutoff = first known scheduled start minus ``offset``.

    The row is kept only if, at that cutoff, the schedule is already known and the start
    known at that time is still after the cutoff. A later reschedule cannot move the cutoff.
    """
    rows: list[tuple[UUID, datetime]] = []
    excluded: Counter[str] = Counter()
    for match_id in match_ids:
        schedules = store.schedules(match_id)
        first = next((item for item in schedules if item.scheduled_start is not None), None)
        if first is None or first.scheduled_start is None:
            excluded["no_schedule"] += 1
            continue
        as_of = first.scheduled_start - offset
        known = AsOfView(store, as_of, mode).schedule(match_id)
        if known is None or known[0].scheduled_start is None:
            excluded["schedule_not_known_at_cutoff"] += 1
            continue
        if known[0].scheduled_start <= as_of:
            excluded["started_before_cutoff"] += 1
            continue
        rows.append((match_id, as_of))
    return rows, excluded


def build_dataset(
    store: IdentityStore,
    feature_set: FeatureSet,
    rows: Sequence[tuple[UUID, datetime]],
    *,
    name: str,
    mode: AvailabilityMode,
    cutoff_rule: str,
    created_at: datetime,
    code_revision: str,
    source_versions: dict[str, str],
    random_seed: int | None = None,
    excluded: Counter[str] | None = None,
) -> tuple[DatasetManifest, tuple[FeatureSnapshot, ...]]:
    snapshots: list[FeatureSnapshot] = []
    skipped: Counter[str] = Counter(excluded or {})
    for match_id, as_of in sorted(rows, key=lambda item: (item[1], str(item[0]))):
        try:
            snapshots.append(build_features(store, match_id, as_of, feature_set, mode=mode))
        except LeakageError:
            skipped["not_pre_match_at_cutoff"] += 1
    classes = [item.availability for item in snapshots]
    counts = {kind: classes.count(kind) for kind in AvailabilityClass}
    availability = worst(classes)
    reproducibility = ReproducibilityInfo(
        code_revision=code_revision,
        tzdata_version=tzdata_version(),
        feature_config_sha256=feature_set.sha256,
        source_versions=source_versions,
        random_seed=random_seed,
    )
    row_refs = tuple((item.match_id, item.as_of, item.snapshot_sha256) for item in snapshots)
    content = digest(
        {
            "rows": [[str(a), b.isoformat(), c] for a, b, c in row_refs],
            "reproducibility": reproducibility.model_dump(mode="json"),
            "mode": mode.value,
            "cutoff_rule": cutoff_rule,
        }
    )
    manifest = DatasetManifest(
        dataset_id=stable_id("dataset", f"{name}:{content}"),
        name=name,
        created_at=created_at,
        mode=mode,
        availability=availability,
        research_only=availability == AvailabilityClass.RESEARCH_ONLY,
        feature_set=feature_set.version,
        feature_set_sha256=feature_set.sha256,
        cutoff_rule=cutoff_rule,
        rows=row_refs,
        excluded=dict(sorted(skipped.items())),
        class_counts=counts,
        reproducibility=reproducibility,
        content_sha256=content,
    )
    return manifest, tuple(snapshots)


def write_dataset(
    root: Path, manifest: DatasetManifest, snapshots: Sequence[FeatureSnapshot]
) -> ArtifactManifest:
    """Write immutable rows and a content-addressed artifact manifest."""
    rows = canonical_json(
        {
            "manifest": manifest.model_dump(mode="json"),
            "snapshots": [item.model_dump(mode="json") for item in snapshots],
        }
    )
    artifact = create_manifest(
        artifact_id=manifest.dataset_id,
        kind=ArtifactKind.DATASET,
        name=manifest.name,
        created_at=manifest.created_at,
        content=rows,
        code_revision=manifest.reproducibility.code_revision,
    )
    store = ArtifactStore(root)
    target = store.write(artifact).parent / "rows.json"
    if target.exists():
        if target.read_bytes() != rows:
            raise FileExistsError("Dataset rows are immutable")
        return artifact
    temporary = target.with_name(f".rows.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(rows)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return artifact
