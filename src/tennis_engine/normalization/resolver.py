"""Evidence-based identity resolution (F04.3–F04.5). It proposes; it never writes.

Scores combine independent evidence as ``1 - prod(1 - weight)``. The policy guarantees
that name evidence alone stays below the review threshold, so a name match can only
create a review item. Conflicting hard attributes exclude a candidate.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from tennis_engine.common.ids import stable_id

from .contracts import (
    Candidate,
    DrawType,
    EventQuery,
    EvidenceItem,
    EvidenceKind,
    Match,
    MatchResolution,
    Player,
    PlayerResolution,
    ResolutionAction,
    ResolutionContext,
    ResolutionDecision,
    ResolutionPolicy,
    SourcePlayerRecord,
)
from .names import NameMatch, compare
from .store import IdentityStore

DEFAULT_POLICY = ResolutionPolicy(
    version="identity-policy-v1-candidate",
    weights={
        EvidenceKind.STABLE_SOURCE_ID: Decimal(1),
        EvidenceKind.NAME_EXACT: Decimal("0.6"),
        EvidenceKind.NAME_INITIALS: Decimal("0.3"),
        EvidenceKind.BIRTH_DATE: Decimal("0.99"),
        EvidenceKind.NATIONALITY: Decimal("0.3"),
        EvidenceKind.TOUR: Decimal(0),
        EvidenceKind.OPPONENT_SCHEDULED_MATCH: Decimal("0.99"),
        EvidenceKind.TOURNAMENT: Decimal("0.3"),
    },
)
NAME_KINDS = frozenset({EvidenceKind.NAME_EXACT, EvidenceKind.NAME_INITIALS})


def combine(policy: ResolutionPolicy, kinds: Iterable[EvidenceKind]) -> Decimal:
    remaining = Decimal(1)
    for kind in set(kinds):
        remaining *= Decimal(1) - policy.weights.get(kind, Decimal(0))
    return Decimal(1) - remaining


def new_player_id(source_id: str, source_player_id: str) -> UUID:
    return stable_id("player", f"{source_id}:{source_player_id}")


class EvidenceResolver:
    """Implements the :class:`~tennis_engine.normalization.contracts.IdentityResolver` protocol."""

    def __init__(self, store: IdentityStore, policy: ResolutionPolicy = DEFAULT_POLICY) -> None:
        self.store = store
        self.policy = policy

    # Player resolution.

    def _known_names(self, player: Player) -> set[str]:
        names = {player.display_name}
        names.update(alias.source_name for alias in self.store.aliases_for_player(player.player_id))
        return names

    def _best_name(self, player: Player, name: str) -> NameMatch:
        best = NameMatch.NONE
        for known in self._known_names(player):
            result = compare(name, known)
            if result == NameMatch.EXACT:
                return result
            if result in {NameMatch.INITIALS, NameMatch.PARTIAL}:
                best = NameMatch.INITIALS if best == NameMatch.INITIALS else result
        return best

    def _scheduled_matches(self, player_id: UUID, start: datetime, at: datetime) -> list[Match]:
        window = timedelta(hours=self.policy.schedule_window_hours)
        found = []
        for match in self.store.matches():
            if player_id not in match.player_ids:
                continue
            known = self.known_start(match.match_id, at)
            if known is not None and abs(known - start) <= window:
                found.append(match)
        return found

    def known_start(self, match_id: UUID, at: datetime) -> datetime | None:
        """Latest scheduled start observed by ``at``; later reschedules are invisible."""
        known = [
            item.scheduled_start
            for item in self.store.schedules(match_id)
            if item.availability.observed_at <= at and item.scheduled_start is not None
        ]
        return known[-1] if known else None

    def _opponent_matches(
        self, candidate: Player, context: ResolutionContext, at: datetime
    ) -> tuple[bool, str]:
        if context.scheduled_start is None:
            return False, ""
        for match in self._scheduled_matches(candidate.player_id, context.scheduled_start, at):
            other_id = next(item for item in match.player_ids if item != candidate.player_id)
            if context.opponent_player_id is not None and other_id == context.opponent_player_id:
                return True, f"scheduled match {match.match_id} with resolved opponent"
            if context.opponent_name is not None:
                other = self.store.player(other_id)
                if self._best_name(other, context.opponent_name) == NameMatch.EXACT:
                    return True, f"scheduled match {match.match_id} with named opponent"
        return False, ""

    def _score_candidate(
        self,
        player: Player,
        record: SourcePlayerRecord,
        context: ResolutionContext | None,
        at: datetime,
        *,
        stable: bool,
    ) -> Candidate:
        evidence: list[EvidenceItem] = []
        conflicts: list[str] = []
        if stable:
            evidence.append(
                EvidenceItem(kind=EvidenceKind.STABLE_SOURCE_ID, detail="active source alias")
            )
        name = self._best_name(player, record.full_name)
        if name == NameMatch.EXACT:
            evidence.append(EvidenceItem(kind=EvidenceKind.NAME_EXACT, detail="folded tokens"))
        elif name == NameMatch.INITIALS:
            evidence.append(EvidenceItem(kind=EvidenceKind.NAME_INITIALS, detail="initials"))
        elif name == NameMatch.PARTIAL:
            evidence.append(EvidenceItem(kind=EvidenceKind.NAME_INITIALS, detail="token subset"))
        if record.tour is not None:
            if record.tour != player.tour:
                conflicts.append(f"tour {record.tour} != {player.tour}")
            else:
                evidence.append(EvidenceItem(kind=EvidenceKind.TOUR, detail=str(record.tour)))
        if record.birth_date is not None and player.birth_date is not None:
            if record.birth_date != player.birth_date:
                conflicts.append("birth date differs")
            else:
                evidence.append(EvidenceItem(kind=EvidenceKind.BIRTH_DATE, detail="equal"))
        if record.nationality is not None and player.nationality is not None:
            # Nationality can change, so a mismatch is not a hard conflict.
            if record.nationality == player.nationality:
                evidence.append(EvidenceItem(kind=EvidenceKind.NATIONALITY, detail="equal"))
        if context is not None:
            found, detail = self._opponent_matches(player, context, at)
            if found:
                evidence.append(
                    EvidenceItem(kind=EvidenceKind.OPPONENT_SCHEDULED_MATCH, detail=detail)
                )
        if conflicts:
            return Candidate(
                entity_id=player.player_id,
                score=Decimal(0),
                decision=ResolutionDecision.REJECT,
                evidence=tuple(evidence),
                conflicts=tuple(conflicts),
            )
        kinds = [item.kind for item in evidence]
        score = combine(self.policy, kinds)
        if not set(kinds) - NAME_KINDS - {EvidenceKind.TOUR}:
            # Guard in code as well as in policy: names alone never pass review.
            score = min(score, self.policy.review_threshold - Decimal("0.01"))
        if score >= self.policy.auto_accept_threshold:
            decision = ResolutionDecision.AUTO_ACCEPT
        elif score >= self.policy.review_threshold:
            decision = ResolutionDecision.REVIEW_REQUIRED
        else:
            decision = ResolutionDecision.REJECT
        return Candidate(
            entity_id=player.player_id,
            score=score,
            decision=decision,
            evidence=tuple(evidence),
        )

    def resolve_player(
        self,
        record: SourcePlayerRecord,
        context: ResolutionContext | None = None,
        *,
        at: datetime,
    ) -> PlayerResolution:
        alias = self.store.player_alias(record.source_id, record.source_player_id, as_of=at)
        ids = self.store.candidate_player_ids(record.full_name)
        if alias is not None:
            ids.add(alias.player_id)
        candidates = sorted(
            (
                self._score_candidate(
                    self.store.player(player_id),
                    record,
                    context,
                    at,
                    stable=alias is not None and alias.player_id == player_id,
                )
                for player_id in ids
            ),
            key=lambda item: (-item.score, str(item.entity_id)),
        )
        reasons: list[str] = []

        def outcome(
            decision: ResolutionDecision, action: ResolutionAction, player_id: UUID | None
        ) -> PlayerResolution:
            return PlayerResolution(
                source_id=record.source_id,
                source_player_id=record.source_player_id,
                source_name=record.full_name,
                decision=decision,
                action=action,
                player_id=player_id,
                candidates=tuple(candidates),
                reasons=tuple(reasons),
                policy_version=self.policy.version,
                resolved_at=at,
            )

        review = ResolutionDecision.REVIEW_REQUIRED
        none = ResolutionAction.NONE
        if alias is not None:
            linked = next(item for item in candidates if item.entity_id == alias.player_id)
            if linked.conflicts:
                reasons.append("source ID is linked but attributes conflict; possible ID reuse")
                return outcome(review, none, None)
            return outcome(
                ResolutionDecision.AUTO_ACCEPT, ResolutionAction.LINK_EXISTING, alias.player_id
            )
        viable = [item for item in candidates if item.decision != ResolutionDecision.REJECT]
        named = [
            item
            for item in candidates
            if not item.conflicts and any(e.kind in NAME_KINDS for e in item.evidence)
        ]
        if viable and viable[0].decision == ResolutionDecision.AUTO_ACCEPT:
            if len(viable) > 1:
                reasons.append("more than one candidate reaches the review threshold")
                return outcome(review, none, None)
            return outcome(
                ResolutionDecision.AUTO_ACCEPT,
                ResolutionAction.LINK_EXISTING,
                viable[0].entity_id,
            )
        if viable:
            reasons.append("best candidate lacks enough corroborating evidence")
            return outcome(review, none, None)
        if named:
            reasons.append("name-only candidates exist; creating a player could duplicate one")
            return outcome(review, none, None)
        if record.source_id in self.policy.allow_create and record.tour is not None:
            reasons.append("no candidate; source may create canonical players")
            return outcome(
                ResolutionDecision.AUTO_ACCEPT,
                ResolutionAction.CREATE_NEW,
                new_player_id(record.source_id, record.source_player_id),
            )
        reasons.append("no accepted candidate and this source cannot create players")
        return outcome(review, none, None)

    # Event resolution.

    def resolve_event(self, query: EventQuery, *, at: datetime) -> MatchResolution:
        first, second = query.participants
        contexts = (
            ResolutionContext(
                opponent_name=second.full_name,
                scheduled_start=query.scheduled_start,
                tournament_name=query.tournament_name,
            ),
            ResolutionContext(
                opponent_name=first.full_name,
                scheduled_start=query.scheduled_start,
                tournament_name=query.tournament_name,
            ),
        )
        people = (
            self.resolve_player(first, contexts[0], at=at),
            self.resolve_player(second, contexts[1], at=at),
        )
        reasons: list[str] = []
        candidates: list[Candidate] = []

        def outcome(match: Match | None, swapped: bool | None) -> MatchResolution:
            accepted = match is not None
            return MatchResolution(
                source_id=query.source_id,
                source_event_id=query.source_event_id,
                decision=(
                    ResolutionDecision.AUTO_ACCEPT
                    if accepted
                    else ResolutionDecision.REVIEW_REQUIRED
                ),
                action=ResolutionAction.LINK_EXISTING if accepted else ResolutionAction.NONE,
                match_id=match.match_id if match else None,
                swapped=swapped,
                participants=people,
                candidates=tuple(candidates),
                reasons=tuple(reasons),
                policy_version=self.policy.version,
                resolved_at=at,
            )

        if query.draw_type != DrawType.SINGLES:
            reasons.append("only singles events are supported")
            return outcome(None, None)
        linked = [
            item.player_id
            for item in people
            if item.decision == ResolutionDecision.AUTO_ACCEPT
            and item.action == ResolutionAction.LINK_EXISTING
        ]
        if len(linked) != 2 or linked[0] == linked[1]:
            reasons.append("both participants must resolve to distinct existing players")
            return outcome(None, None)
        pair = {linked[0], linked[1]}
        alias = self.store.match_alias(query.source_id, query.source_event_id)
        if alias is not None:
            aliased = self.store.match(alias.match_id)
            if set(aliased.player_ids) != pair:
                reasons.append("participants differ from the mapped match; possible replacement")
                return outcome(None, None)
            return outcome(aliased, aliased.player_ids[0] != linked[0])
        matches = []
        for match in self.store.matches():
            if set(match.player_ids) != pair:
                continue
            if query.tour is not None and match.tour != query.tour:
                continue
            if query.scheduled_start is not None:
                window = timedelta(hours=self.policy.schedule_window_hours)
                known = self.known_start(match.match_id, at)
                if known is None or abs(known - query.scheduled_start) > window:
                    continue
            matches.append(match)
        for match in matches:
            candidates.append(
                Candidate(
                    entity_id=match.match_id,
                    score=Decimal(1) if len(matches) == 1 else Decimal("0.5"),
                    decision=(
                        ResolutionDecision.AUTO_ACCEPT
                        if len(matches) == 1
                        else ResolutionDecision.REVIEW_REQUIRED
                    ),
                    evidence=(
                        EvidenceItem(kind=EvidenceKind.STABLE_SOURCE_ID, detail="both players"),
                    ),
                )
            )
        if query.scheduled_start is None:
            reasons.append("an event without a scheduled start cannot be mapped automatically")
            return outcome(None, None)
        if len(matches) != 1:
            reasons.append(f"{len(matches)} canonical matches fit the players and schedule")
            return outcome(None, None)
        match = matches[0]
        return outcome(match, match.player_ids[0] != linked[0])
