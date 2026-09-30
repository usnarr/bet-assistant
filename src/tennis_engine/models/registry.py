"""F11.8 model registry and F13.9 audited champion switch and rollback.

A bundle is the unit of promotion and rollback. It names every file that scoring needs:
the base model artifact, its manifest and model card, an optional calibrator and its
manifest, and the evaluation report. Each file has a SHA-256. The bundle also records the
feature set and its hash, the code revision, the dependency lock hash, the training cutoff
and the rollback target. The bundle ID is a hash of this content.

Rules:

- Registration reads the files and checks that they belong together: the card describes
  the artifact, the calibrator names the same base artifact, and the feature set of the
  artifact and the card agree. A mixed bundle is refused.
- A champion switch needs a `PASS` promotion decision (F13.9) for the bundle's candidate,
  an approving reviewer who is not the author, a switching reviewer who is not the author,
  and the rollback target named in the decision. A decision promotes once. Nothing in the
  code calls a switch on its own; only the reviewed CLI command does.
- A rollback restores one complete bundle: the declared rollback target of the current
  champion, or an earlier champion of the same family. The target must verify. "No
  champion" is always a valid target, because then scoring abstains.
- Loading verifies every hash again. A missing or changed file refuses the bundle, so
  scoring abstains. There is no fallback to another version.

The registry stores events append-only. A switch and a rollback are events with the
actor, the role, the reason, the decision and any warning. Nothing is deleted.
"""

import hashlib
import json
import threading
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.backtesting.contracts import GateStatus
from tennis_engine.backtesting.promotion import PromotionGate, ReleaseDecision
from tennis_engine.common.clock import Clock, require_aware
from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp
from tennis_engine.common.ids import stable_id
from tennis_engine.features.contracts import canonical_json
from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.infrastructure.artifacts import ArtifactManifest

from .baselines.contracts import BaselineArtifact, ModelCard
from .calibration.contracts import CalibratorArtifact

NO_CHAMPION = "no-champion"
CodeRevision = Annotated[str, Field(min_length=7, max_length=64, pattern=r"^[0-9a-f]+$")]


class RegistryRefused(ValueError):
    """The registry refused an operation. `reasons` are codes without secrets or paths."""

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__(";".join(self.reasons))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FileRef(Contract):
    """A file under the artifact root. The path is relative and POSIX."""

    path: Annotated[str, Field(min_length=1, max_length=512)]
    sha256: Digest

    @model_validator(mode="after")
    def relative(self) -> Self:
        parts = PurePosixPath(self.path)
        if parts.is_absolute() or ".." in parts.parts or "\\" in self.path or ":" in self.path:
            raise ValueError("A registry path is relative to the artifact root")
        return self

    def resolve(self, root: Path) -> Path:
        base = root.resolve()
        target = base.joinpath(*PurePosixPath(self.path).parts).resolve()
        if base not in target.parents:
            raise ValueError("A registry path escapes the artifact root")
        return target


class ModelBundle(Contract):
    schema_version: Literal["1.0"] = "1.0"
    bundle_id: UUID
    family: Identifier
    version: Identifier
    candidate: Identifier
    model: FileRef
    model_manifest: FileRef
    model_card: FileRef
    calibrator: FileRef | None
    calibrator_manifest: FileRef | None
    evaluation_report: FileRef
    model_artifact_sha256: Digest
    calibrator_artifact_sha256: Digest | None
    feature_set: Identifier
    feature_set_sha256: Digest
    code_revision: CodeRevision
    dependency_lock_sha256: Digest
    training_cutoff: Timestamp
    rollback_target: UUID | None
    registered_by: Identifier
    registered_at: Timestamp
    content_sha256: Digest

    @model_validator(mode="after")
    def calibrator_pair(self) -> Self:
        pair = (self.calibrator, self.calibrator_manifest, self.calibrator_artifact_sha256)
        if any(item is None for item in pair) and any(item is not None for item in pair):
            raise ValueError("A calibrator needs its file, its manifest and its hash")
        if self.rollback_target == self.bundle_id:
            raise ValueError("A bundle cannot be its own rollback target")
        return self

    @property
    def rollback_reference(self) -> str:
        """The value that the promotion decision must name as its rollback target."""
        return str(self.rollback_target) if self.rollback_target else NO_CHAMPION

    def files(self) -> dict[str, FileRef]:
        found = {
            "model": self.model,
            "model_manifest": self.model_manifest,
            "model_card": self.model_card,
            "evaluation_report": self.evaluation_report,
        }
        if self.calibrator is not None and self.calibrator_manifest is not None:
            found["calibrator"] = self.calibrator
            found["calibrator_manifest"] = self.calibrator_manifest
        return found


def _content(values: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(values)).hexdigest()


def _ref(root: Path, path: Path) -> FileRef:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return FileRef(path=relative, sha256=sha256_file(path))


class LoadedBundle(Contract):
    bundle: ModelBundle
    artifact: BaselineArtifact
    card: ModelCard
    calibrator: CalibratorArtifact | None


def _consistency(
    artifact: BaselineArtifact,
    card: ModelCard,
    manifest: ArtifactManifest,
    model_bytes: bytes,
    calibrator: CalibratorArtifact | None,
    calibrator_manifest: ArtifactManifest | None,
    calibrator_bytes: bytes | None,
) -> list[str]:
    problems = []
    if card.artifact_sha256 != artifact.artifact_sha256 or card.model != artifact.name:
        problems.append("CARD_DESCRIBES_ANOTHER_MODEL")
    if (card.feature_set, card.feature_set_sha256) != (
        artifact.feature_set,
        artifact.feature_set_sha256,
    ):
        problems.append("FEATURE_SET_MISMATCH")
    if card.training_cutoff != artifact.training_cutoff:
        problems.append("TRAINING_CUTOFF_MISMATCH")
    if manifest.content_sha256 != hashlib.sha256(model_bytes).hexdigest():
        problems.append("MODEL_MANIFEST_MISMATCH")
    if calibrator is not None:
        if (calibrator.base_model, calibrator.base_artifact_sha256) != (
            artifact.name,
            artifact.artifact_sha256,
        ):
            problems.append("CALIBRATOR_BELONGS_TO_ANOTHER_MODEL")
        if calibrator.base_training_cutoff != artifact.training_cutoff:
            problems.append("CALIBRATOR_TRAINING_CUTOFF_MISMATCH")
        if (
            calibrator_manifest is None
            or calibrator_bytes is None
            or (calibrator_manifest.content_sha256 != hashlib.sha256(calibrator_bytes).hexdigest())
        ):
            problems.append("CALIBRATOR_MANIFEST_MISMATCH")
    return problems


def build_bundle(
    root: Path,
    *,
    family: str,
    version: str,
    model_dir: Path,
    calibrator_dir: Path | None,
    evaluation_report: Path,
    rollback_target: UUID | None,
    registered_by: str,
    registered_at: datetime,
) -> ModelBundle:
    """Describe stored F09 baseline and F11 calibrator files as one bundle."""
    model_path, card_path = model_dir / "artifact.json", model_dir / "model-card.json"
    manifest_path = model_dir / "manifest.json"
    model_bytes = model_path.read_bytes()
    artifact = BaselineArtifact.model_validate_json(model_bytes)
    card = ModelCard.model_validate_json(card_path.read_bytes())
    manifest = ArtifactManifest.model_validate_json(manifest_path.read_bytes())
    calibrator = calibrator_manifest = calibrator_bytes = None
    if calibrator_dir is not None:
        calibrator_bytes = (calibrator_dir / "calibrator.json").read_bytes()
        calibrator = CalibratorArtifact.model_validate_json(calibrator_bytes)
        calibrator_manifest = ArtifactManifest.model_validate_json(
            (calibrator_dir / "manifest.json").read_bytes()
        )
    problems = _consistency(
        artifact, card, manifest, model_bytes, calibrator, calibrator_manifest, calibrator_bytes
    )
    if problems:
        raise RegistryRefused(problems)
    values: dict[str, object] = {
        "family": family,
        "version": version,
        "candidate": artifact.name,
        "model": _ref(root, model_path),
        "model_manifest": _ref(root, manifest_path),
        "model_card": _ref(root, card_path),
        "calibrator": _ref(root, calibrator_dir / "calibrator.json") if calibrator_dir else None,
        "calibrator_manifest": (
            _ref(root, calibrator_dir / "manifest.json") if calibrator_dir else None
        ),
        "evaluation_report": _ref(root, evaluation_report),
        "model_artifact_sha256": artifact.artifact_sha256,
        "calibrator_artifact_sha256": calibrator.artifact_sha256 if calibrator else None,
        "feature_set": artifact.feature_set,
        "feature_set_sha256": artifact.feature_set_sha256,
        "code_revision": card.code_revision,
        "dependency_lock_sha256": card.dependency_lock_sha256,
        "training_cutoff": artifact.training_cutoff,
        "rollback_target": rollback_target,
        "registered_by": registered_by,
        "registered_at": require_aware(registered_at),
    }
    dumped = {
        key: value.model_dump(mode="json") if isinstance(value, Contract) else value
        for key, value in values.items()
    }
    content = _content(json.loads(json.dumps(dumped, default=str)))
    return ModelBundle.model_validate(
        values | {"bundle_id": stable_id("model-bundle", content), "content_sha256": content}
    )


def verify_bundle(root: Path, bundle: ModelBundle) -> tuple[str, ...]:
    """Every hash and every cross-reference, read again from disk."""
    problems: list[str] = []
    contents: dict[str, bytes] = {}
    for name, ref in bundle.files().items():
        try:
            data = ref.resolve(root).read_bytes()
        except (OSError, ValueError):
            problems.append(f"FILE_MISSING:{name}")
            continue
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            problems.append(f"HASH_MISMATCH:{name}")
            continue
        contents[name] = data
    if problems:
        return tuple(problems)
    try:
        artifact = BaselineArtifact.model_validate_json(contents["model"])
        card = ModelCard.model_validate_json(contents["model_card"])
        manifest = ArtifactManifest.model_validate_json(contents["model_manifest"])
        calibrator = calibrator_manifest = None
        if "calibrator" in contents:
            calibrator = CalibratorArtifact.model_validate_json(contents["calibrator"])
            calibrator_manifest = ArtifactManifest.model_validate_json(
                contents["calibrator_manifest"]
            )
    except ValueError:
        return ("CONTENT_INVALID",)
    problems = _consistency(
        artifact,
        card,
        manifest,
        contents["model"],
        calibrator,
        calibrator_manifest,
        contents.get("calibrator"),
    )
    if artifact.artifact_sha256 != bundle.model_artifact_sha256:
        problems.append("MODEL_NOT_REGISTERED_VERSION")
    if (calibrator.artifact_sha256 if calibrator else None) != bundle.calibrator_artifact_sha256:
        problems.append("CALIBRATOR_NOT_REGISTERED_VERSION")
    if (artifact.feature_set, artifact.feature_set_sha256) != (
        bundle.feature_set,
        bundle.feature_set_sha256,
    ):
        problems.append("FEATURE_SET_NOT_REGISTERED_VERSION")
    return tuple(problems)


def load_bundle(root: Path, bundle: ModelBundle) -> LoadedBundle:
    problems = verify_bundle(root, bundle)
    if problems:
        raise RegistryRefused(problems)
    files = bundle.files()
    return LoadedBundle(
        bundle=bundle,
        artifact=BaselineArtifact.model_validate_json(files["model"].resolve(root).read_bytes()),
        card=ModelCard.model_validate_json(files["model_card"].resolve(root).read_bytes()),
        calibrator=(
            CalibratorArtifact.model_validate_json(files["calibrator"].resolve(root).read_bytes())
            if "calibrator" in files
            else None
        ),
    )


class EventKind(StrEnum):
    PROMOTE = "PROMOTE"
    ROLLBACK = "ROLLBACK"


class ChampionEvent(Contract):
    event_id: UUID
    family: Identifier
    sequence: Annotated[int, Field(ge=1, strict=True)]
    kind: EventKind
    bundle_id: UUID | None
    previous_bundle_id: UUID | None
    decision_id: UUID | None
    decision_sha256: Digest | None
    actor: Annotated[str, Field(min_length=1, max_length=128)]
    actor_role: Role
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    warnings: tuple[str, ...] = ()
    recorded_at: Timestamp


class StaleChampion(RuntimeError):
    """Another switch changed the champion first. Read again and retry."""


class RegistryStore(Protocol):
    def register(self, bundle: ModelBundle) -> bool:
        """Insert the bundle. False when it exists with the same content."""
        ...

    def bundle(self, bundle_id: UUID) -> ModelBundle | None: ...

    def events(self, family: str) -> tuple[ChampionEvent, ...]: ...

    def append(self, event: ChampionEvent, expected_current: UUID | None) -> None:
        """Append atomically, only when the current champion is `expected_current`."""
        ...


def _current(events: tuple[ChampionEvent, ...]) -> UUID | None:
    return events[-1].bundle_id if events else None


class InMemoryRegistryStore:
    def __init__(self) -> None:
        self._bundles: dict[UUID, ModelBundle] = {}
        self._events: dict[str, list[ChampionEvent]] = {}
        self._lock = threading.Lock()

    def register(self, bundle: ModelBundle) -> bool:
        with self._lock:
            existing = self._bundles.get(bundle.bundle_id)
            if existing is not None:
                if existing != bundle:
                    raise RegistryRefused(("BUNDLE_ID_CONFLICT",))
                return False
            if any(
                (item.family, item.version) == (bundle.family, bundle.version)
                for item in self._bundles.values()
            ):
                raise RegistryRefused(("VERSION_ALREADY_REGISTERED",))
            self._bundles[bundle.bundle_id] = bundle
            return True

    def bundle(self, bundle_id: UUID) -> ModelBundle | None:
        return self._bundles.get(bundle_id)

    def events(self, family: str) -> tuple[ChampionEvent, ...]:
        return tuple(self._events.get(family, ()))

    def append(self, event: ChampionEvent, expected_current: UUID | None) -> None:
        with self._lock:
            history = self._events.setdefault(event.family, [])
            if _current(tuple(history)) != expected_current:
                raise StaleChampion("The champion changed")
            if event.sequence != len(history) + 1:
                raise StaleChampion("The event sequence changed")
            if event.decision_id is not None and any(
                item.decision_id == event.decision_id for item in history
            ):
                raise RegistryRefused(("DECISION_ALREADY_USED",))
            history.append(event)


SWITCH_ROLES = (Role.POLICY_REVIEWER,)
ROLLBACK_ROLES = (Role.POLICY_REVIEWER, Role.OPERATOR)


class ModelRegistry:
    """Registration, the champion switch and rollback. Every call checks the files."""

    def __init__(self, store: RegistryStore, root: Path, clock: Clock) -> None:
        self.store = store
        self.root = root
        self.clock = clock

    def register(self, bundle: ModelBundle) -> bool:
        problems = list(verify_bundle(self.root, bundle))
        if bundle.rollback_target is not None:
            target = self.store.bundle(bundle.rollback_target)
            if target is None:
                problems.append("ROLLBACK_TARGET_UNKNOWN")
            elif target.family != bundle.family:
                problems.append("ROLLBACK_TARGET_OTHER_FAMILY")
        if problems:
            raise RegistryRefused(problems)
        return self.store.register(bundle)

    def champion(self, family: str) -> ModelBundle | None:
        current = _current(self.store.events(family))
        return self.store.bundle(current) if current else None

    def _target_problems(
        self, bundle: ModelBundle, supported_feature_sets: frozenset[str]
    ) -> list[str]:
        problems = list(verify_bundle(self.root, bundle))
        if bundle.feature_set_sha256 not in supported_feature_sets:
            problems.append("FEATURE_SET_NOT_SUPPORTED")
        return problems

    def _event(
        self,
        family: str,
        kind: EventKind,
        bundle_id: UUID | None,
        previous: UUID | None,
        actor: Principal,
        reason: str,
        decision: ReleaseDecision | None,
        warnings: tuple[str, ...],
    ) -> ChampionEvent:
        events = self.store.events(family)
        now = require_aware(self.clock.now())
        sequence = len(events) + 1
        return ChampionEvent(
            event_id=stable_id("champion-event", f"{family}:{sequence}:{now.isoformat()}"),
            family=family,
            sequence=sequence,
            kind=kind,
            bundle_id=bundle_id,
            previous_bundle_id=previous,
            decision_id=decision.decision_id if decision else None,
            decision_sha256=decision.content_sha256 if decision else None,
            actor=actor.identity,
            actor_role=actor.role,
            reason=reason,
            warnings=warnings,
            recorded_at=now,
        )

    def promote(
        self,
        bundle_id: UUID,
        decision: ReleaseDecision,
        *,
        actor: Principal,
        reason: str,
        supported_feature_sets: frozenset[str],
        runtime_lock_sha256: str | None = None,
    ) -> ChampionEvent:
        bundle = self.store.bundle(bundle_id)
        if bundle is None:
            raise RegistryRefused(("BUNDLE_UNKNOWN",))
        problems: list[str] = []
        if actor.role not in SWITCH_ROLES:
            problems.append("ROLE_CANNOT_SWITCH")
        if not reason.strip():
            problems.append("REASON_REQUIRED")
        if decision.status != GateStatus.PASS:
            problems.append(f"DECISION_NOT_PASS:{decision.status.value}")
        failed = sorted(g.gate.value for g in decision.gates if g.status != GateStatus.PASS)
        if failed:
            problems.append(f"GATES_NOT_PASS:{','.join(failed)}")
        review = next((g for g in decision.gates if g.gate == PromotionGate.REVIEW), None)
        if review is None or review.status != GateStatus.PASS or decision.reviewer is None:
            problems.append("NO_APPROVING_REVIEW")
        elif decision.reviewer == decision.author:
            problems.append("REVIEWER_IS_AUTHOR")
        if actor.identity == decision.author:
            problems.append("AUTHOR_CANNOT_SWITCH")
        if decision.decision_id != stable_id("release-decision", decision.content_sha256):
            problems.append("DECISION_ID_MISMATCH")
        if decision.candidate != bundle.candidate:
            problems.append("DECISION_FOR_ANOTHER_CANDIDATE")
        if decision.rollback_target != bundle.rollback_reference:
            problems.append("DECISION_NAMES_ANOTHER_ROLLBACK_TARGET")
        problems += self._target_problems(bundle, supported_feature_sets)
        if bundle.rollback_target is not None:
            target = self.store.bundle(bundle.rollback_target)
            if target is None:
                problems.append("ROLLBACK_TARGET_UNKNOWN")
            else:
                problems += [
                    f"ROLLBACK_TARGET:{item}"
                    for item in self._target_problems(target, supported_feature_sets)
                ]
        events = self.store.events(bundle.family)
        if any(item.decision_id == decision.decision_id for item in events):
            problems.append("DECISION_ALREADY_USED")
        current = _current(events)
        if current == bundle.bundle_id:
            problems.append("ALREADY_CHAMPION")
        if problems:
            raise RegistryRefused(problems)
        warnings: tuple[str, ...] = ()
        if runtime_lock_sha256 and runtime_lock_sha256 != bundle.dependency_lock_sha256:
            warnings = ("DEPENDENCY_LOCK_DIFFERS_FROM_RUNTIME",)
        event = self._event(
            bundle.family,
            EventKind.PROMOTE,
            bundle.bundle_id,
            current,
            actor,
            reason,
            decision,
            warnings,
        )
        self.store.append(event, current)
        return event

    def rollback(
        self,
        family: str,
        *,
        actor: Principal,
        reason: str,
        supported_feature_sets: frozenset[str],
        target: UUID | Literal["declared", "no-champion"] = "declared",
        runtime_lock_sha256: str | None = None,
    ) -> ChampionEvent:
        events = self.store.events(family)
        current_id = _current(events)
        problems: list[str] = []
        if actor.role not in ROLLBACK_ROLES:
            problems.append("ROLE_CANNOT_ROLL_BACK")
        if not reason.strip():
            problems.append("REASON_REQUIRED")
        current = self.store.bundle(current_id) if current_id else None
        if current is None:
            raise RegistryRefused([*problems, "NO_CHAMPION"])
        wanted: UUID | None
        if target == "declared":
            wanted = current.rollback_target
        elif isinstance(target, UUID):
            wanted = target
        else:
            wanted = None
        warnings: tuple[str, ...] = ()
        if wanted is not None:
            earlier = {item.bundle_id for item in events[:-1]}
            if wanted != current.rollback_target and wanted not in earlier:
                problems.append("ROLLBACK_TARGET_NOT_ALLOWED")
            bundle = self.store.bundle(wanted)
            if bundle is None:
                problems.append("ROLLBACK_TARGET_UNKNOWN")
            else:
                if bundle.family != family:
                    problems.append("ROLLBACK_TARGET_OTHER_FAMILY")
                problems += [
                    f"ROLLBACK_TARGET:{item}"
                    for item in self._target_problems(bundle, supported_feature_sets)
                ]
                if runtime_lock_sha256 and runtime_lock_sha256 != bundle.dependency_lock_sha256:
                    warnings = ("DEPENDENCY_LOCK_DIFFERS_FROM_RUNTIME",)
        if problems:
            raise RegistryRefused(problems)
        event = self._event(
            family, EventKind.ROLLBACK, wanted, current_id, actor, reason, None, warnings
        )
        self.store.append(event, current_id)
        return event

    def active(self, family: str, supported_feature_sets: frozenset[str]) -> LoadedBundle | None:
        """The verified champion bundle, or None (no champion: scoring abstains).

        A champion that fails verification raises `RegistryRefused`. The caller must then
        abstain; it must not load another version.
        """
        bundle = self.champion(family)
        if bundle is None:
            return None
        if bundle.feature_set_sha256 not in supported_feature_sets:
            raise RegistryRefused(("FEATURE_SET_NOT_SUPPORTED",))
        return load_bundle(self.root, bundle)
