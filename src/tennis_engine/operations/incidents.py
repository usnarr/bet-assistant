"""F15.7 incident records with preserved evidence and affected recommendation IDs.

An incident bundle is a new immutable directory under `<artifact_root>/incidents/<id>/`:

- `incident.json`: the record, with the affected recommendation IDs and applied stops.
- `decisions.jsonl`: the affected stored decisions, unchanged, one per line.
- `governance.json`: the F01 journal export at the time of the incident.
- `manifest.json`: the SHA-256 of each file above.

Opening an incident never changes or deletes a decision. A correction is a new F12 record
version. The bundle can hold licensed values, so keep it in access-controlled storage and
never commit it.
"""

import hashlib
import json
import os
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from tennis_engine.common.clock import require_aware
from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp
from tennis_engine.common.ids import stable_id
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.serving.contracts import StoredDecision
from tennis_engine.serving.store import DecisionQuery, DecisionStore

PAGE = 500


class Category(StrEnum):
    """The F15.8 runbooks, plus the blueprint's stale-publication critical alert."""

    SOURCE_FAILURE = "SOURCE_FAILURE"
    PARSER_DRIFT = "PARSER_DRIFT"
    IDENTITY_ERROR = "IDENTITY_ERROR"
    BAD_SETTLEMENT = "BAD_SETTLEMENT"
    FUTURE_LEAKAGE = "FUTURE_LEAKAGE"
    COMPROMISED_ARTIFACT = "COMPROMISED_ARTIFACT"
    RISK_STORE_OUTAGE = "RISK_STORE_OUTAGE"
    STALE_PUBLICATION = "STALE_PUBLICATION"


class StopApplied(Contract):
    scope: Literal["source", "global"]
    target: str
    revision: int | None
    outcome: Literal["APPLIED", "ALREADY_STOPPED"]


class IncidentRecord(Contract):
    incident_id: UUID
    category: Category
    severity: Literal["CRITICAL", "WARNING"]
    summary: str = Field(min_length=1, max_length=2000)
    opened_at: Timestamp
    opened_by: str
    window_start: Timestamp
    window_end: Timestamp
    source_ids: tuple[Identifier, ...]
    bookmakers: tuple[Identifier, ...]
    affected_recommendation_ids: tuple[UUID, ...]
    stops: tuple[StopApplied, ...]
    alert_rule_ids: tuple[Identifier, ...] = ()


class BundleManifest(Contract):
    incident_id: UUID
    files: dict[str, Digest]


def affected(
    store: DecisionStore,
    *,
    window_start: datetime,
    window_end: datetime,
    source_ids: Iterable[str] = (),
    bookmakers: Iterable[str] = (),
) -> tuple[StoredDecision, ...]:
    """Every stored version decided in [start, end) that uses a named source or bookmaker.

    With no source and no bookmaker, every decision in the window is affected.
    """
    start, end = require_aware(window_start), require_aware(window_end)
    if end <= start:
        raise ValueError("The incident window must be nonempty")
    sources, books = set(source_ids), set(bookmakers)
    found: list[StoredDecision] = []
    after = None
    while True:
        page = store.query(DecisionQuery(limit=PAGE, latest_only=False, after=after))
        for item in page:
            record = item.record
            in_window = start <= record.decided_at < end
            everything = not sources and not books
            uses = bool(sources & set(item.context.source_ids)) or record.bookmaker in books
            if in_window and (everything or uses):
                found.append(item)
        if len(page) < PAGE:
            break
        last = page[-1]
        after = (last.scheduled_start, last.record.decision_id)
    return tuple(sorted(found, key=lambda item: str(item.record.decision_id)))


def apply_stops(
    governance: GovernanceStore, *, source_ids: Iterable[str], global_stop: bool, reason: str
) -> tuple[StopApplied, ...]:
    """Operator stops through the F01 journal. Already stopped targets append nothing."""
    now = governance.clock()
    stops: list[StopApplied] = []
    for source_id in sorted(set(source_ids)):
        if governance.source_stopped(source_id, now):
            stops.append(
                StopApplied(
                    scope="source", target=source_id, revision=None, outcome="ALREADY_STOPPED"
                )
            )
        else:
            revision = governance.set_source_stop(source_id, True, reason=reason)
            stops.append(
                StopApplied(scope="source", target=source_id, revision=revision, outcome="APPLIED")
            )
    if global_stop:
        if governance.global_disabled(now):
            stops.append(
                StopApplied(scope="global", target="all", revision=None, outcome="ALREADY_STOPPED")
            )
        else:
            revision = governance.set_global_disable(True, reason=reason)
            stops.append(
                StopApplied(scope="global", target="all", revision=revision, outcome="APPLIED")
            )
    return tuple(stops)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def open_incident(
    *,
    root: Path,
    store: DecisionStore,
    governance: GovernanceStore,
    category: Category,
    severity: Literal["CRITICAL", "WARNING"],
    summary: str,
    opened_at: datetime,
    window_start: datetime,
    window_end: datetime,
    source_ids: Iterable[str] = (),
    bookmakers: Iterable[str] = (),
    stop_sources: bool = False,
    global_stop: bool = False,
    alert_rule_ids: Iterable[str] = (),
) -> tuple[IncidentRecord, Path]:
    """Stop first, then preserve evidence. The bundle directory must not exist yet."""
    sources, books = tuple(sorted(set(source_ids))), tuple(sorted(set(bookmakers)))
    opened_at = require_aware(opened_at)
    incident_id = stable_id(
        "incident", f"{category}|{opened_at.isoformat()}|{','.join(sources)}|{summary}"
    )
    reason = f"F15 incident {incident_id} ({category.value})"
    stops = apply_stops(
        governance,
        source_ids=sources if stop_sources else (),
        global_stop=global_stop,
        reason=reason,
    )
    items = affected(
        store,
        window_start=window_start,
        window_end=window_end,
        source_ids=sources,
        bookmakers=books,
    )
    record = IncidentRecord(
        incident_id=incident_id,
        category=category,
        severity=severity,
        summary=summary,
        opened_at=opened_at,
        opened_by=governance.principal.identity,
        window_start=window_start,
        window_end=window_end,
        source_ids=sources,
        bookmakers=books,
        affected_recommendation_ids=tuple(item.record.decision_id for item in items),
        stops=stops,
        alert_rule_ids=tuple(alert_rule_ids),
    )
    directory = root / "incidents" / str(incident_id)
    directory.mkdir(parents=True, exist_ok=False)
    contents = {
        "incident.json": _canonical(record.model_dump(mode="json")),
        "decisions.jsonl": b"".join(_canonical(item.model_dump(mode="json")) for item in items),
        "governance.json": _canonical(governance.export()),
    }
    for name, content in contents.items():
        _write_new(directory / name, content)
    manifest = BundleManifest(
        incident_id=incident_id,
        files={name: hashlib.sha256(content).hexdigest() for name, content in contents.items()},
    )
    _write_new(directory / "manifest.json", _canonical(manifest.model_dump(mode="json")))
    return record, directory


def verify_bundle(directory: Path) -> tuple[str, ...]:
    """Problems with a bundle; empty means every file matches the manifest."""
    manifest = BundleManifest.model_validate_json((directory / "manifest.json").read_bytes())
    problems = []
    for name, expected in sorted(manifest.files.items()):
        path = directory / name
        if not path.is_file():
            problems.append(f"MISSING:{name}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            problems.append(f"HASH_MISMATCH:{name}")
    return tuple(problems)
