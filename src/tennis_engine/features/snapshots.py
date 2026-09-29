"""``build_features(match_id, as_of, feature_set)`` and immutable snapshots (F07.2, F07.6).

A feature group reads only through :class:`AsOfView`, returns values and the exact inputs
it used, and never sees the target match result. Snapshots are content-addressed; storing
a different snapshot under an existing key fails.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID

from tennis_engine.common.contracts import ReasonCode
from tennis_engine.common.errors import EngineError
from tennis_engine.contracts.domain import AvailabilityClass, FeatureValue
from tennis_engine.normalization.contracts import Match, MatchStatus, Player, TournamentEdition
from tennis_engine.normalization.store import IdentityStore

from .asof import AsOfView
from .contracts import (
    AvailabilityMode,
    FeatureDefinition,
    FeatureSnapshot,
    InputRef,
    digest,
    snapshot_digest,
    worst,
)

QUANTUM = Decimal("0.000001")


def fixed(value: Decimal | int) -> Decimal:
    """Deterministic decimal representation for feature values."""
    return Decimal(value).quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass
class FeatureContext:
    view: AsOfView
    match: Match
    edition: TournamentEdition
    players: tuple[Player, Player]
    inputs: list[InputRef] = field(default_factory=list)

    @property
    def as_of(self) -> datetime:
        return self.view.as_of

    def use(self, ref: InputRef) -> None:
        self.inputs.append(ref)

    def static_ref(self, kind: str, key: str, created_at: datetime, source_id: str) -> bool:
        """Record a static attribute (player, match, edition) if it was known by cutoff.

        Returns ``False`` when the mode cannot prove the record was known; the caller must
        then treat the attribute as missing.
        """
        if created_at <= self.as_of:
            proven = AvailabilityClass.PROSPECTIVE
        elif self.view.mode == AvailabilityMode.RESEARCH_ONLY:
            proven = AvailabilityClass.RESEARCH_ONLY
        else:
            return False
        self.use(
            InputRef(
                kind=kind,
                key=key,
                version=1,
                availability=proven,
                observed_at=created_at,
                source_id=source_id,
            )
        )
        return True


GroupValues = dict[str, FeatureValue]
FeatureGroup = Callable[[FeatureContext], GroupValues]


@dataclass(frozen=True)
class FeatureSet:
    version: str
    definitions: tuple[FeatureDefinition, ...]
    groups: tuple[tuple[str, FeatureGroup], ...]
    config: dict[str, str] = field(default_factory=dict)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.definitions)

    @property
    def sha256(self) -> str:
        return digest(
            {
                "version": self.version,
                "definitions": [item.model_dump(mode="json") for item in self.definitions],
                "groups": [name for name, _ in self.groups],
                "config": self.config,
            }
        )


class LeakageError(EngineError):
    pass


def _leak(message: str) -> LeakageError:
    return LeakageError(ReasonCode.INVALID_INPUT, message)


def build_features(
    store: IdentityStore,
    match_id: UUID,
    as_of: datetime,
    feature_set: FeatureSet,
    *,
    mode: AvailabilityMode = AvailabilityMode.PROSPECTIVE,
) -> FeatureSnapshot:
    view = AsOfView(store, as_of, mode)
    match = store.match(match_id)
    if view.result(match_id) is not None:
        raise _leak("The target result is already known at the cutoff; not a pre-match row")
    status = view.status(match_id)
    if status is not None and status[0].status not in {
        MatchStatus.SCHEDULED,
        MatchStatus.POSTPONED,
    }:
        raise _leak(f"Target status {status[0].status} at the cutoff is not pre-match")
    players = (store.player(match.player_ids[0]), store.player(match.player_ids[1]))
    context = FeatureContext(view, match, store.edition(match.edition_id), players)
    values: dict[str, FeatureValue] = {}
    for group_name, group in feature_set.groups:
        produced = group(context)
        unknown = set(produced) - set(feature_set.names)
        if unknown:
            raise ValueError(f"Group {group_name} returned undeclared features {sorted(unknown)}")
        duplicate = set(produced) & set(values)
        if duplicate:
            raise ValueError(f"Features produced twice: {sorted(duplicate)}")
        values.update(produced)
    absent = set(feature_set.names) - set(values)
    if absent:
        raise ValueError(f"Feature set declares features no group produced: {sorted(absent)}")
    inputs = tuple(
        sorted(
            {item.model_dump_json(): item for item in context.inputs}.values(),
            key=lambda item: (item.kind, item.key, item.version),
        )
    )
    ordered = {name: values[name] for name in feature_set.names}
    missing = tuple(name for name in feature_set.names if ordered[name] is None)
    sha = snapshot_digest(
        match_id,
        view.as_of.isoformat(),
        feature_set.version,
        feature_set.sha256,
        mode,
        ordered,
        missing,
        inputs,
    )
    return FeatureSnapshot(
        match_id=match_id,
        as_of=view.as_of,
        feature_set=feature_set.version,
        feature_set_sha256=feature_set.sha256,
        mode=mode,
        availability=worst([item.availability for item in inputs]),
        player_ids=match.player_ids,
        values=ordered,
        missing=missing,
        inputs=inputs,
        snapshot_sha256=sha,
    )


class SnapshotStore:
    """Immutable snapshot store keyed by (match, cutoff, feature-set version)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, datetime, str], FeatureSnapshot] = {}

    def put(self, snapshot: FeatureSnapshot) -> FeatureSnapshot:
        key = (snapshot.match_id, snapshot.as_of, snapshot.feature_set)
        existing = self._rows.get(key)
        if existing is not None:
            if existing.snapshot_sha256 != snapshot.snapshot_sha256:
                raise ValueError("A stored snapshot cannot be replaced; use a new version")
            return existing
        self._rows[key] = snapshot
        return snapshot

    def get(self, match_id: UUID, as_of: datetime, feature_set: str) -> FeatureSnapshot:
        return self._rows[(match_id, as_of, feature_set)]

    def all(self) -> tuple[FeatureSnapshot, ...]:
        return tuple(self._rows.values())
