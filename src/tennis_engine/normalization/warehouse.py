"""Canonical sports warehouse writes, review queue and manual decisions (F04.4–F04.7).

Only accepted resolutions write aliases. Everything else opens a review item. Facts are
appended as new versions only when their content changes, so reprocessing is idempotent
and a provider correction keeps the earlier version. Each public operation is one store
transaction, so a failure part way through writes no rows.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, Concatenate
from uuid import UUID

from tennis_engine.common.clock import Clock
from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import Availability

from .contracts import (
    BestOf,
    DrawStage,
    EditionFormatVersion,
    EvidenceKind,
    Match,
    MatchAlias,
    Player,
    PlayerAlias,
    PlayerResolution,
    RankingSnapshot,
    ResolutionAction,
    ResolutionDecision,
    ResultVersion,
    ReviewState,
    Round,
    ScheduleVersion,
    SetScore,
    SourceFormatRecord,
    SourceMatchRecord,
    SourcePlayerRecord,
    SourceRankingRecord,
    SourceTournamentRecord,
    StatsVersion,
    StatusVersion,
    Tournament,
    TournamentEdition,
)
from .resolver import EvidenceResolver, new_player_id
from .store import AuditEntry, IdentityStore, ReviewItem, ReviewKind, TournamentAlias
from .validation import Issue, errors, validate_match, validate_tournament

SYSTEM_ACTOR = "system:f04-warehouse"


@dataclass(frozen=True)
class IngestOutcome:
    """Result of one source record. ``entity_id`` is set only when it was accepted."""

    accepted: bool
    entity_id: UUID | None
    review_id: UUID | None
    issues: tuple[Issue, ...] = ()
    changed: bool = False


def canonical_order(first: UUID, second: UUID) -> tuple[tuple[UUID, UUID], bool]:
    """Return the canonical pair and whether the source order was swapped."""
    if str(first) <= str(second):
        return (first, second), False
    return (second, first), True


def orient_set(score: SetScore, swapped: bool) -> SetScore:
    if not swapped:
        return score
    points = score.tiebreak_points
    return SetScore(
        games=(score.games[1], score.games[0]),
        tiebreak_points=(points[1], points[0]) if points is not None else None,
    )


def _atomic[**P, R](
    method: Callable[Concatenate["SportsWarehouse", P], R],
) -> Callable[Concatenate["SportsWarehouse", P], R]:
    @wraps(method)
    def run(warehouse: "SportsWarehouse", /, *args: P.args, **kwargs: P.kwargs) -> R:
        with warehouse.store.transaction():
            return method(warehouse, *args, **kwargs)

    return run


class SportsWarehouse:
    def __init__(self, store: IdentityStore, resolver: EvidenceResolver, clock: Clock) -> None:
        self.store = store
        self.resolver = resolver
        self.clock = clock

    # Review queue.

    def _open_review(
        self,
        kind: ReviewKind,
        source_id: str,
        source_key: str,
        reasons: tuple[str, ...],
        payload: dict[str, Any],
    ) -> UUID:
        review_id = stable_id("identity-review", f"{kind}:{source_id}:{source_key}")
        try:
            current = self.store.review(review_id)
        except KeyError:
            current = None
        if current is not None and current.state == ReviewState.OPEN:
            if current.payload == payload and current.reasons == reasons:
                return review_id
        revision = 1 if current is None else current.revision + 1
        self.store.append_review(
            ReviewItem(
                review_id=review_id,
                revision=revision,
                kind=kind,
                source_id=source_id,
                source_key=source_key,
                state=ReviewState.OPEN,
                reasons=reasons,
                payload=payload,
                recorded_at=self.clock.now(),
                actor=SYSTEM_ACTOR,
            )
        )
        self.store.audit(
            AuditEntry(
                self.clock.now(),
                SYSTEM_ACTOR,
                "review.opened",
                f"{kind}:{source_id}:{source_key}",
                "; ".join(reasons),
            )
        )
        return review_id

    def _close_review(
        self, review_id: UUID, state: ReviewState, reviewer: str, reason: str
    ) -> ReviewItem:
        current = self.store.review(review_id)
        if current.state != ReviewState.OPEN:
            raise ValueError(f"Review {review_id} is already {current.state}")
        closed = ReviewItem(
            review_id=review_id,
            revision=current.revision + 1,
            kind=current.kind,
            source_id=current.source_id,
            source_key=current.source_key,
            state=state,
            reasons=current.reasons,
            payload=current.payload,
            recorded_at=self.clock.now(),
            actor=reviewer,
            resolution_note=reason,
        )
        self.store.append_review(closed)
        self.store.audit(
            AuditEntry(
                self.clock.now(), reviewer, f"review.{state.lower()}", str(review_id), reason
            )
        )
        return closed

    # Players.

    def _write_player_alias(
        self,
        record: SourcePlayerRecord,
        player_id: UUID,
        decision: ResolutionDecision,
        evidence: tuple[EvidenceKind, ...],
        reviewer: str | None,
    ) -> PlayerAlias:
        history = self.store.player_alias_history(record.source_id, record.source_player_id)
        previous = history[-1] if history else None
        if (
            previous is not None
            and previous.active
            and previous.player_id == player_id
            and previous.source_name == record.full_name
        ):
            return previous
        version = len(history) + 1
        alias = PlayerAlias(
            alias_id=stable_id(
                "player-alias", f"{record.source_id}:{record.source_player_id}:{version}"
            ),
            player_id=player_id,
            source_id=record.source_id,
            source_player_id=record.source_player_id,
            source_name=record.full_name,
            version=version,
            active=True,
            decision=decision,
            reviewed_by=reviewer,
            evidence=evidence,
            recorded_at=self.clock.now(),
            supersedes=previous.alias_id if previous else None,
        )
        return self.store.append_player_alias(alias)

    def _create_player(self, record: SourcePlayerRecord, player_id: UUID) -> Player:
        if record.tour is None:
            raise ValueError("A canonical player requires a known tour")
        return self.store.add_player(
            Player(
                player_id=player_id,
                tour=record.tour,
                display_name=record.full_name,
                birth_date=record.birth_date,
                nationality=record.nationality,
                handedness=record.handedness,
                created_at=self.clock.now(),
            )
        )

    @_atomic
    def ingest_player(self, record: SourcePlayerRecord) -> IngestOutcome:
        resolution = self.resolver.resolve_player(record, at=self.clock.now())
        return self.apply_player_resolution(record, resolution)

    @_atomic
    def apply_player_resolution(
        self, record: SourcePlayerRecord, resolution: PlayerResolution
    ) -> IngestOutcome:
        if resolution.decision != ResolutionDecision.AUTO_ACCEPT or resolution.player_id is None:
            review_id = self._open_review(
                ReviewKind.PLAYER,
                record.source_id,
                record.source_player_id,
                resolution.reasons,
                {
                    "record": record.model_dump(mode="json"),
                    "resolution": resolution.model_dump(mode="json", exclude={"resolved_at"}),
                },
            )
            return IngestOutcome(False, None, review_id)
        if resolution.action == ResolutionAction.CREATE_NEW:
            self._create_player(record, resolution.player_id)
            evidence: tuple[EvidenceKind, ...] = (EvidenceKind.STABLE_SOURCE_ID,)
        else:
            best = next(c for c in resolution.candidates if c.entity_id == resolution.player_id)
            evidence = tuple(item.kind for item in best.evidence)
        before = len(self.store.player_alias_history(record.source_id, record.source_player_id))
        alias = self._write_player_alias(
            record, resolution.player_id, ResolutionDecision.AUTO_ACCEPT, evidence, None
        )
        return IngestOutcome(True, alias.player_id, None, changed=alias.version > before)

    @_atomic
    def approve_player(
        self,
        review_id: UUID,
        *,
        reviewer: str,
        reason: str,
        player_id: UUID | None = None,
        create: bool = False,
    ) -> PlayerAlias:
        """Manual decision: link to ``player_id`` or create a new player. It is audited."""
        if reviewer.startswith("system:") or reviewer.startswith("agent:"):
            raise PermissionError("Only a human reviewer can approve an identity review")
        if (player_id is None) == (not create):
            raise ValueError("Choose exactly one of player_id or create")
        item = self.store.review(review_id)
        if item.kind != ReviewKind.PLAYER:
            raise ValueError("Not a player review")
        record = SourcePlayerRecord.model_validate(item.payload["record"])
        if player_id is None:
            target = new_player_id(record.source_id, record.source_player_id)
            self._create_player(record, target)
        else:
            target = self.store.player(player_id).player_id
        alias = self._write_player_alias(
            record,
            target,
            ResolutionDecision.REVIEW_REQUIRED,
            (EvidenceKind.MANUAL_REVIEW,),
            reviewer,
        )
        self._close_review(review_id, ReviewState.APPROVED, reviewer, reason)
        return alias

    @_atomic
    def reject_review(self, review_id: UUID, *, reviewer: str, reason: str) -> ReviewItem:
        return self._close_review(review_id, ReviewState.REJECTED, reviewer, reason)

    @_atomic
    def remap_player_alias(
        self,
        source_id: str,
        source_player_id: str,
        new_player_id: UUID,
        *,
        reviewer: str,
        reason: str,
    ) -> tuple[PlayerAlias, tuple[UUID, ...]]:
        """Correct a mapping with a new alias version. Return matches to revalidate."""
        if reviewer.startswith("system:") or reviewer.startswith("agent:"):
            raise PermissionError("Only a human reviewer can change an identity mapping")
        history = self.store.player_alias_history(source_id, source_player_id)
        if not history:
            raise KeyError("No alias to correct")
        previous = history[-1]
        self.store.player(new_player_id)
        record = SourcePlayerRecord(
            source_id=source_id,
            source_player_id=source_player_id,
            full_name=previous.source_name,
        )
        alias = self._write_player_alias(
            record,
            new_player_id,
            ResolutionDecision.REVIEW_REQUIRED,
            (EvidenceKind.MANUAL_REVIEW,),
            reviewer,
        )
        affected = tuple(
            match.match_id
            for match in self.store.matches()
            if previous.player_id in match.player_ids or new_player_id in match.player_ids
        )
        self.store.audit(
            AuditEntry(
                self.clock.now(),
                reviewer,
                "alias.remapped",
                f"{source_id}:{source_player_id}",
                reason,
                {
                    "from": str(previous.player_id),
                    "to": str(new_player_id),
                    "revalidate_matches": [str(item) for item in affected],
                },
            )
        )
        return alias, affected

    # Tournaments.

    @_atomic
    def ingest_tournament(self, record: SourceTournamentRecord) -> IngestOutcome:
        issues = validate_tournament(record)
        if errors(issues) or record.tour is None:
            review_id = self._open_review(
                ReviewKind.TOURNAMENT,
                record.source_id,
                f"{record.source_tournament_id}:{record.season}",
                tuple(issue.code for issue in errors(issues)),
                {"record": record.model_dump(mode="json")},
            )
            return IngestOutcome(False, None, review_id, issues)
        existing = self.store.tournament_alias(
            record.source_id, record.source_tournament_id, record.season
        )
        if existing is not None:
            return IngestOutcome(True, existing.edition_id, None, issues)
        if record.source_id not in self.resolver.policy.allow_create:
            review_id = self._open_review(
                ReviewKind.TOURNAMENT,
                record.source_id,
                f"{record.source_tournament_id}:{record.season}",
                ("source cannot create tournaments",),
                {"record": record.model_dump(mode="json")},
            )
            return IngestOutcome(False, None, review_id, issues)
        tournament_id = stable_id("tournament", f"{record.source_id}:{record.source_tournament_id}")
        edition_id = stable_id(
            "edition", f"{record.source_id}:{record.source_tournament_id}:{record.season}"
        )
        self.store.add_tournament(
            Tournament(
                tournament_id=tournament_id,
                tour=record.tour,
                name=record.name,
                level=record.level,
            )
        )
        self.store.add_edition(
            TournamentEdition(
                edition_id=edition_id,
                tournament_id=tournament_id,
                season=record.season,
                surface=record.surface,
                environment=record.environment,
                timezone=record.timezone,
                start_date=record.start_date,
                end_date=record.end_date,
            )
        )
        self.store.add_tournament_alias(
            TournamentAlias(
                record.source_id,
                record.source_tournament_id,
                record.season,
                tournament_id,
                edition_id,
                self.clock.now(),
            )
        )
        return IngestOutcome(True, edition_id, None, issues, changed=True)

    # Matches.

    @_atomic
    def ingest_match(self, record: SourceMatchRecord, availability: Availability) -> IngestOutcome:
        key = record.source_match_id
        issues = validate_match(record)
        payload = {"record": record.model_dump(mode="json")}
        if errors(issues):
            reasons = tuple(issue.code for issue in errors(issues))
            review_id = self._open_review(
                ReviewKind.RECORD, record.source_id, key, reasons, payload
            )
            return IngestOutcome(False, None, review_id, issues)
        assert record.tour is not None
        aliases = [
            self.store.player_alias(record.source_id, source_player_id)
            for source_player_id in record.participant_ids
        ]
        edition = self.store.tournament_alias(
            record.source_id, record.source_tournament_id, record.season
        )
        reasons_list = []
        if any(alias is None for alias in aliases):
            reasons_list.append("unresolved participant blocks the match")
        if edition is None:
            reasons_list.append("unresolved tournament edition")
        if reasons_list:
            review_id = self._open_review(
                ReviewKind.MATCH, record.source_id, key, tuple(reasons_list), payload
            )
            return IngestOutcome(False, None, review_id, issues)
        assert edition is not None
        first, second = (alias.player_id for alias in aliases if alias is not None)
        if first == second:
            review_id = self._open_review(
                ReviewKind.MATCH,
                record.source_id,
                key,
                ("both participants resolve to one player",),
                payload,
            )
            return IngestOutcome(False, None, review_id, issues)
        pair, swapped = canonical_order(first, second)
        match_alias = self.store.match_alias(record.source_id, key)
        changed = False
        if match_alias is not None:
            match = self.store.match(match_alias.match_id)
            if match.player_ids != pair:
                review_id = self._open_review(
                    ReviewKind.MATCH,
                    record.source_id,
                    key,
                    ("participants changed; replacement opponent requires review",),
                    payload,
                )
                return IngestOutcome(False, None, review_id, issues)
        else:
            match = self._find_or_create_match(record, edition.edition_id, pair)
            self.store.append_match_alias(
                MatchAlias(
                    alias_id=stable_id("match-alias", f"{record.source_id}:{key}:1"),
                    match_id=match.match_id,
                    source_id=record.source_id,
                    source_match_id=key,
                    version=1,
                    active=True,
                    decision=ResolutionDecision.AUTO_ACCEPT,
                    swapped=swapped,
                    recorded_at=self.clock.now(),
                )
            )
            changed = True
        changed |= self._append_facts(match, record, availability, swapped)
        return IngestOutcome(True, match.match_id, None, issues, changed)

    def _find_or_create_match(
        self, record: SourceMatchRecord, edition_id: UUID, pair: tuple[UUID, UUID]
    ) -> Match:
        for match in self.store.matches():
            # Link only across sources. Two IDs from one source are two matches, and a
            # round-robin can repeat a pairing, so neither case may merge.
            if (
                match.edition_id == edition_id
                and match.player_ids == pair
                and match.round == record.round
                and record.round not in {Round.RR, Round.UNKNOWN}
                and match.draw_stage == record.draw_stage
                and record.source_id not in self.store.match_alias_sources(match.match_id)
            ):
                return match
        assert record.tour is not None
        return self.store.add_match(
            Match(
                match_id=stable_id("match", f"{record.source_id}:{record.source_match_id}"),
                edition_id=edition_id,
                tour=record.tour,
                draw_type=record.draw_type,
                draw_stage=record.draw_stage,
                round=record.round,
                best_of=record.best_of,
                player_ids=pair,
                created_at=self.clock.now(),
            )
        )

    def _append_facts(
        self,
        match: Match,
        record: SourceMatchRecord,
        availability: Availability,
        swapped: bool,
    ) -> bool:
        changed = False
        match_id = match.match_id
        schedules = self.store.schedules(match_id)
        if record.scheduled_start is not None and (
            not schedules or schedules[-1].scheduled_start != record.scheduled_start
        ):
            self.store.append_schedule(
                ScheduleVersion(
                    match_id=match_id,
                    version=len(schedules) + 1,
                    scheduled_start=record.scheduled_start,
                    source_id=record.source_id,
                    availability=availability,
                )
            )
            changed = True
        statuses = self.store.statuses(match_id)
        state = (record.status, record.actual_start, record.actual_end)
        if (
            not statuses
            or (
                statuses[-1].status,
                statuses[-1].actual_start,
                statuses[-1].actual_end,
            )
            != state
        ):
            self.store.append_status(
                StatusVersion(
                    match_id=match_id,
                    version=len(statuses) + 1,
                    status=record.status,
                    actual_start=record.actual_start,
                    actual_end=record.actual_end,
                    source_id=record.source_id,
                    availability=availability,
                )
            )
            changed = True
        if record.status.has_winner and record.winner_id is not None:
            alias = self.store.player_alias(record.source_id, record.winner_id)
            assert alias is not None
            sets = tuple(orient_set(score, swapped) for score in record.sets)
            results = self.store.results(match_id)
            latest = results[-1] if results else None
            if latest is None or (latest.status, latest.winner_id, latest.sets) != (
                record.status,
                alias.player_id,
                sets,
            ):
                self.store.append_result(
                    ResultVersion(
                        match_id=match_id,
                        version=len(results) + 1,
                        status=record.status,
                        winner_id=alias.player_id,
                        sets=sets,
                        source_id=record.source_id,
                        availability=availability,
                        corrects_version=latest.version if latest else None,
                    )
                )
                changed = True
        for source_player_id, counts in zip(record.participant_ids, record.stats, strict=True):
            if counts is None:
                continue
            alias = self.store.player_alias(record.source_id, source_player_id)
            assert alias is not None
            history = self.store.stats(match_id, alias.player_id)
            if not history or history[-1].counts != counts:
                self.store.append_stats(
                    StatsVersion(
                        match_id=match_id,
                        player_id=alias.player_id,
                        version=len(history) + 1,
                        counts=counts,
                        source_id=record.source_id,
                        availability=availability,
                    )
                )
                changed = True
        return changed

    # Rankings.

    @_atomic
    def ingest_format(
        self, record: SourceFormatRecord, availability: Availability
    ) -> IngestOutcome:
        """Append a deciding-set rule version when it differs from the latest version."""
        alias = self.store.tournament_alias(
            record.source_id, record.source_tournament_id, record.season
        )
        key = f"{record.source_tournament_id}:{record.season}:{record.draw_stage}:{record.best_of}"
        if (
            alias is None
            or record.draw_stage == DrawStage.UNKNOWN
            or (record.best_of == BestOf.UNKNOWN)
        ):
            reason = (
                "format rule for an unresolved edition"
                if alias is None
                else "format rule for an unknown draw stage or best-of format"
            )
            review_id = self._open_review(
                ReviewKind.RECORD,
                record.source_id,
                f"format:{key}",
                (reason,),
                {"record": record.model_dump(mode="json")},
            )
            return IngestOutcome(False, None, review_id)
        history = self.store.edition_formats(alias.edition_id, record.draw_stage, record.best_of)
        latest = history[-1] if history else None
        if latest is not None and (latest.deciding_set, latest.reference) == (
            record.deciding_set,
            record.reference,
        ):
            return IngestOutcome(True, alias.edition_id, None)
        self.store.append_edition_format(
            EditionFormatVersion(
                edition_id=alias.edition_id,
                draw_stage=record.draw_stage,
                best_of=record.best_of,
                version=len(history) + 1,
                deciding_set=record.deciding_set,
                reference=record.reference,
                source_id=record.source_id,
                availability=availability,
                corrects_version=None if latest is None else latest.version,
            )
        )
        return IngestOutcome(True, alias.edition_id, None, changed=True)

    @_atomic
    def ingest_ranking(
        self, record: SourceRankingRecord, availability: Availability
    ) -> IngestOutcome:
        alias = self.store.player_alias(record.source_id, record.source_player_id)
        if alias is None:
            review_id = self._open_review(
                ReviewKind.RANKING,
                record.source_id,
                f"{record.source_player_id}:{record.ranking_date.isoformat()}",
                ("ranking for an unresolved player",),
                {"record": record.model_dump(mode="json")},
            )
            return IngestOutcome(False, None, review_id)
        snapshot, created = self.store.add_ranking(
            RankingSnapshot(
                player_id=alias.player_id,
                tour=record.tour,
                ranking_date=record.ranking_date,
                rank=record.rank,
                points=record.points,
                source_id=record.source_id,
                availability=availability,
            )
        )
        return IngestOutcome(True, snapshot.player_id, None, changed=created)
