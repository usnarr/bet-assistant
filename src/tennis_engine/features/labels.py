"""Evaluation labels, kept apart from prediction inputs (F07.3).

A label uses the latest corrected result. It never flows back into a feature snapshot,
so a correction after a cutoff cannot change what the model saw.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from tennis_engine.common.contracts import Contract, Timestamp
from tennis_engine.normalization.contracts import MatchStatus
from tennis_engine.normalization.store import IdentityStore


class MatchLabel(Contract):
    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    player_one_won: bool
    status: MatchStatus
    result_version: int
    corrected: bool
    observed_at: Timestamp


def final_label(store: IdentityStore, match_id: UUID) -> MatchLabel | None:
    """Latest result for evaluation. ``None`` means no label exists yet."""
    return _label(store, match_id, None)


def label_known_at(store: IdentityStore, match_id: UUID, at: datetime) -> MatchLabel | None:
    """Label as our own observations knew it at ``at``; used to fit models chronologically."""
    return _label(store, match_id, at)


def _label(store: IdentityStore, match_id: UUID, at: datetime | None) -> MatchLabel | None:
    results = [
        item
        for item in store.results(match_id)
        if at is None or item.availability.observed_at <= at
    ]
    if not results:
        return None
    latest = results[-1]
    match = store.match(match_id)
    return MatchLabel(
        match_id=match_id,
        player_one_won=latest.winner_id == match.player_ids[0],
        status=latest.status,
        result_version=latest.version,
        corrected=latest.version > 1,
        observed_at=latest.availability.observed_at,
    )
