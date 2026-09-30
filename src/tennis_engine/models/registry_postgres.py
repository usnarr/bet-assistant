"""PostgreSQL model registry (migration 0013). Both tables are append-only.

A champion event is appended under a transaction-scoped advisory lock on its family. The
store reads the last event inside the lock and appends only when the champion is still
the one that the caller checked, so two concurrent switches cannot both win.
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from .registry import ChampionEvent, ModelBundle, RegistryRefused, StaleChampion


class PostgresRegistryStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def register(self, bundle: ModelBundle) -> bool:
        body = bundle.model_dump_json()
        with self.engine.begin() as db:
            existing = db.execute(
                text("SELECT bundle FROM tennis.model_bundle WHERE bundle_id = :id"),
                {"id": bundle.bundle_id},
            ).first()
            if existing is not None:
                if ModelBundle.model_validate(existing.bundle) != bundle:
                    raise RegistryRefused(("BUNDLE_ID_CONFLICT",))
                return False
            try:
                db.execute(
                    text(
                        "INSERT INTO tennis.model_bundle (bundle_id, family, version, "
                        "content_sha256, bundle, registered_at) VALUES (:id, :family, "
                        ":version, :sha, CAST(:bundle AS JSONB), :at)"
                    ),
                    {
                        "id": bundle.bundle_id,
                        "family": bundle.family,
                        "version": bundle.version,
                        "sha": bundle.content_sha256,
                        "bundle": body,
                        "at": bundle.registered_at,
                    },
                )
            except IntegrityError:
                raise RegistryRefused(("VERSION_ALREADY_REGISTERED",)) from None
        return True

    def bundle(self, bundle_id: UUID) -> ModelBundle | None:
        with self.engine.connect() as db:
            row = db.execute(
                text("SELECT bundle FROM tennis.model_bundle WHERE bundle_id = :id"),
                {"id": bundle_id},
            ).first()
        return ModelBundle.model_validate(row.bundle) if row is not None else None

    def events(self, family: str) -> tuple[ChampionEvent, ...]:
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT event FROM tennis.champion_event WHERE family = :family "
                    "ORDER BY sequence"
                ),
                {"family": family},
            ).all()
        return tuple(ChampionEvent.model_validate(row.event) for row in rows)

    def append(self, event: ChampionEvent, expected_current: UUID | None) -> None:
        params: dict[str, Any] = {
            "family": event.family,
            "sequence": event.sequence,
            "event_id": event.event_id,
            "kind": event.kind.value,
            "bundle_id": event.bundle_id,
            "previous": event.previous_bundle_id,
            "decision_id": event.decision_id,
            "decision_sha": event.decision_sha256,
            "event": json.dumps(event.model_dump(mode="json"), sort_keys=True),
            "at": event.recorded_at,
        }
        with self.engine.begin() as db:
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('champion:' || :family))"),
                {"family": event.family},
            )
            last = db.execute(
                text(
                    "SELECT sequence, bundle_id FROM tennis.champion_event "
                    "WHERE family = :family ORDER BY sequence DESC LIMIT 1"
                ),
                {"family": event.family},
            ).first()
            current = last.bundle_id if last is not None else None
            sequence = last.sequence if last is not None else 0
            if current != expected_current or event.sequence != sequence + 1:
                raise StaleChampion("The champion changed")
            try:
                db.execute(
                    text(
                        "INSERT INTO tennis.champion_event (family, sequence, event_id, kind, "
                        "bundle_id, previous_bundle_id, decision_id, decision_sha256, event, "
                        "recorded_at) VALUES (:family, :sequence, :event_id, :kind, :bundle_id, "
                        ":previous, :decision_id, :decision_sha, CAST(:event AS JSONB), :at)"
                    ),
                    params,
                )
            except IntegrityError:
                raise RegistryRefused(("DECISION_ALREADY_USED",)) from None
