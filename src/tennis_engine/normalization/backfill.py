"""Resumable backfill from F03 derived records and coverage reports (F04.6, F04.8).

Input order is deterministic, so a checkpoint is a position in that order. Each fact gets
its observation time from the F03 observation, never from the event date. A historical
import without verified archive evidence therefore stays research-only for replay.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from tennis_engine.common.clock import Clock
from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp
from tennis_engine.contracts.domain import Availability
from tennis_engine.ingestion.contracts import ObservationRecord, ParsedItem
from tennis_engine.ingestion.store import DerivedRecord, MemoryIngestionStore

from .contracts import (
    MatchStatus,
    SourceMatchRecord,
    SourcePlayerRecord,
    SourceRankingRecord,
    SourceTournamentRecord,
)
from .store import IdentityStore
from .warehouse import IngestOutcome, SportsWarehouse

RECORD_ORDER = {
    "sports-player": 0,
    "sports-tournament": 1,
    "sports-match": 2,
    "sports-ranking": 3,
}


class ArchiveEvidence(Contract):
    """Reviewed proof that the source published a record by a historical time."""

    source_available_at: Timestamp
    evidence_id: Identifier


class SyntheticWarehousePayload(Contract):
    schema_version: Literal["1.0"]
    fixture_id: Identifier
    players: tuple[SourcePlayerRecord, ...] = ()
    tournaments: tuple[SourceTournamentRecord, ...] = ()
    matches: tuple[SourceMatchRecord, ...] = ()
    rankings: tuple[SourceRankingRecord, ...] = ()
    archive_evidence: dict[str, ArchiveEvidence] = Field(default_factory=dict)


class SyntheticWarehouseParser:
    """Strict parser for synthetic F04 fixtures. It is not a real provider adapter."""

    version = "synthetic-warehouse-v1"

    def parse(self, body: bytes) -> tuple[ParsedItem, ...]:
        payload = SyntheticWarehousePayload.model_validate_json(body)
        items: list[ParsedItem] = []

        def add(record_type: str, key: str, record: Contract) -> None:
            data: dict[str, Any] = {"record": record.model_dump(mode="json")}
            evidence = payload.archive_evidence.get(key)
            if evidence is not None:
                data["archive_evidence"] = evidence.model_dump(mode="json")
            items.append(ParsedItem(record_type=record_type, natural_key=key, payload=data))

        for player in payload.players:
            add("sports-player", f"player:{player.source_player_id}", player)
        for tournament in payload.tournaments:
            key = f"tournament:{tournament.source_tournament_id}:{tournament.season}"
            add("sports-tournament", key, tournament)
        for match in payload.matches:
            add("sports-match", f"match:{match.source_match_id}", match)
        for ranking in payload.rankings:
            key = f"ranking:{ranking.source_player_id}:{ranking.ranking_date.isoformat()}"
            add("sports-ranking", key, ranking)
        return tuple(items)


@dataclass(frozen=True)
class SourceFact:
    record: DerivedRecord
    observation: ObservationRecord


def facts_from_ingestion(store: MemoryIngestionStore, parser_version: str) -> list[SourceFact]:
    """Pair derived records with their observations in a deterministic order."""
    facts = [
        SourceFact(record, store.observations[record.observation_id])
        for record in store.derived_records.values()
        if record.parser_version == parser_version and record.record_type in RECORD_ORDER
    ]
    return sorted(
        facts,
        key=lambda fact: (
            fact.observation.observed_at,
            RECORD_ORDER[fact.record.record_type],
            fact.record.natural_key,
            str(fact.record.record_id),
        ),
    )


class SeasonCoverage(Contract):
    season: int
    tour: Identifier
    matches: int = 0
    with_result: int = 0
    with_both_stats: int = 0
    missing_stats: int = 0
    unknown_format: int = 0
    unresolved: int = 0


class BackfillReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    checkpoint: Identifier
    parser_version: Identifier
    started_at: Timestamp
    completed_at: Timestamp
    start_position: int
    end_position: int
    total_inputs: int
    accepted: dict[str, int]
    review_required: dict[str, int]
    changed: int
    coverage: tuple[SeasonCoverage, ...]
    facts_without_archive_evidence: int
    facts_with_archive_evidence: int
    review_ids: tuple[UUID, ...]
    report_sha256: Digest | None = None

    def sealed(self) -> "BackfillReport":
        body = self.model_dump(mode="json", exclude={"report_sha256"})
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self.model_copy(update={"report_sha256": digest})


def _effective(record: Contract) -> datetime | None:
    if isinstance(record, SourceMatchRecord):
        return record.actual_end or record.scheduled_start
    if isinstance(record, SourceRankingRecord):
        return datetime.combine(record.ranking_date, time(0), tzinfo=UTC)
    return None


class Backfill:
    def __init__(self, warehouse: SportsWarehouse, store: IdentityStore, clock: Clock) -> None:
        self.warehouse = warehouse
        self.store = store
        self.clock = clock

    def run(
        self,
        facts: Sequence[SourceFact],
        *,
        checkpoint: str,
        parser_version: str,
        batch_size: int = 500,
        max_batches: int | None = None,
    ) -> BackfillReport:
        if batch_size < 1:
            raise ValueError("Batch size must be positive")
        started = self.clock.now()
        start = self.store.checkpoint(checkpoint)
        position = start
        accepted: Counter[str] = Counter()
        review: Counter[str] = Counter()
        review_ids: list[UUID] = []
        changed = 0
        archived = unarchived = 0
        batches = 0
        while position < len(facts) and (max_batches is None or batches < max_batches):
            batch = facts[position : position + batch_size]
            # The batch and its checkpoint commit together, so a resumed run never
            # skips a fact or applies half a batch.
            with self.store.transaction():
                outcomes = [(fact, *self._apply(fact)) for fact in batch]
                self.store.save_checkpoint(checkpoint, position + len(batch))
            for fact, outcome, has_evidence in outcomes:
                kind = fact.record.record_type
                if outcome.accepted:
                    accepted[kind] += 1
                else:
                    review[kind] += 1
                    if outcome.review_id is not None:
                        review_ids.append(outcome.review_id)
                changed += int(outcome.changed)
                if kind in {"sports-match", "sports-ranking"}:
                    archived += int(has_evidence)
                    unarchived += int(not has_evidence)
            position += len(batch)
            batches += 1
        return BackfillReport(
            checkpoint=checkpoint,
            parser_version=parser_version,
            started_at=started,
            completed_at=self.clock.now(),
            start_position=start,
            end_position=position,
            total_inputs=len(facts),
            accepted=dict(sorted(accepted.items())),
            review_required=dict(sorted(review.items())),
            changed=changed,
            coverage=coverage(self.store, facts),
            facts_with_archive_evidence=archived,
            facts_without_archive_evidence=unarchived,
            review_ids=tuple(dict.fromkeys(review_ids)),
        ).sealed()

    def _availability(self, fact: SourceFact, record: Contract) -> tuple[Availability, bool]:
        evidence = fact.record.payload.get("archive_evidence")
        parsed = ArchiveEvidence.model_validate(evidence) if evidence is not None else None
        return (
            Availability(
                observed_at=fact.observation.observed_at,
                ingested_at=self.clock.now(),
                effective_at=_effective(record),
                source_available_at=parsed.source_available_at if parsed else None,
                availability_evidence_id=parsed.evidence_id if parsed else None,
            ),
            parsed is not None,
        )

    def _apply(self, fact: SourceFact) -> tuple[IngestOutcome, bool]:
        data = fact.record.payload["record"]
        kind = fact.record.record_type
        if kind == "sports-player":
            return self.warehouse.ingest_player(SourcePlayerRecord.model_validate(data)), False
        if kind == "sports-tournament":
            record = SourceTournamentRecord.model_validate(data)
            return self.warehouse.ingest_tournament(record), False
        if kind == "sports-match":
            match = SourceMatchRecord.model_validate(data)
            availability, evidence = self._availability(fact, match)
            return self.warehouse.ingest_match(match, availability), evidence
        ranking = SourceRankingRecord.model_validate(data)
        availability, evidence = self._availability(fact, ranking)
        return self.warehouse.ingest_ranking(ranking, availability), evidence


def coverage(store: IdentityStore, facts: Iterable[SourceFact]) -> tuple[SeasonCoverage, ...]:
    """Per season/tour completeness of source matches, including unresolved ones."""
    latest: dict[tuple[str, str], SourceMatchRecord] = {}
    for fact in facts:
        if fact.record.record_type == "sports-match":
            record = SourceMatchRecord.model_validate(fact.record.payload["record"])
            latest[(record.source_id, record.source_match_id)] = record
    rows: dict[tuple[int, str], dict[str, int]] = {}
    for record in latest.values():
        row = rows.setdefault((record.season, str(record.tour or "UNKNOWN")), Counter())
        row["matches"] += 1
        alias = store.match_alias(record.source_id, record.source_match_id)
        if alias is None:
            row["unresolved"] += 1
            continue
        if record.best_of.sets_to_win is None:
            row["unknown_format"] += 1
        if store.results(alias.match_id):
            row["with_result"] += 1
        if record.status in {MatchStatus.COMPLETED, MatchStatus.RETIRED}:
            match = store.match(alias.match_id)
            if all(store.stats(alias.match_id, player) for player in match.player_ids):
                row["with_both_stats"] += 1
            else:
                row["missing_stats"] += 1
    return tuple(
        SeasonCoverage(season=season, tour=tour.lower(), **counts)
        for (season, tour), counts in sorted(rows.items())
    )
