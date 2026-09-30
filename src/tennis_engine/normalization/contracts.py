"""F04 contracts for canonical players, tournaments, matches and identity resolution.

Source records keep provider text unchanged. Canonical records use internal UUIDs. Every
versioned fact carries an :class:`Availability` so F07 can decide what was known at a
cutoff. ``UNKNOWN`` values are explicit and never mean a default.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, Identifier, Probability, Timestamp
from tennis_engine.contracts.domain import Availability, Tour

NonNegative = Annotated[int, Field(ge=0, strict=True)]
SourceKey = Annotated[str, Field(min_length=1, max_length=256)]
SourceText = Annotated[str, Field(min_length=1, max_length=512)]
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class Surface(StrEnum):
    HARD = "HARD"
    CLAY = "CLAY"
    GRASS = "GRASS"
    CARPET = "CARPET"
    UNKNOWN = "UNKNOWN"


class CourtEnvironment(StrEnum):
    INDOOR = "INDOOR"
    OUTDOOR = "OUTDOOR"
    UNKNOWN = "UNKNOWN"


class CompetitionLevel(StrEnum):
    GRAND_SLAM = "GRAND_SLAM"
    TOUR = "TOUR"
    CHALLENGER = "CHALLENGER"
    ITF = "ITF"
    UNKNOWN = "UNKNOWN"


class DrawType(StrEnum):
    SINGLES = "SINGLES"
    DOUBLES = "DOUBLES"


class DrawStage(StrEnum):
    QUALIFYING = "QUALIFYING"
    MAIN = "MAIN"
    UNKNOWN = "UNKNOWN"


class Round(StrEnum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    R128 = "R128"
    R64 = "R64"
    R32 = "R32"
    R16 = "R16"
    QF = "QF"
    SF = "SF"
    F = "F"
    RR = "RR"
    UNKNOWN = "UNKNOWN"


class BestOf(StrEnum):
    THREE = "BEST_OF_3"
    FIVE = "BEST_OF_5"
    UNKNOWN = "UNKNOWN"

    @property
    def sets_to_win(self) -> int | None:
        return {BestOf.THREE: 2, BestOf.FIVE: 3}.get(self)


class DecidingSetRule(StrEnum):
    """How the final set of a match is decided. ``UNKNOWN`` blocks format-based models."""

    TIEBREAK_7 = "TIEBREAK_7"  # Normal set, 7-point tiebreak at 6-6.
    TIEBREAK_10 = "TIEBREAK_10"  # Normal set, 10-point tiebreak at 6-6.
    MATCH_TIEBREAK_10 = "MATCH_TIEBREAK_10"  # A 10-point tiebreak replaces the final set.
    ADVANTAGE = "ADVANTAGE"  # No tiebreak; win by two games.
    UNKNOWN = "UNKNOWN"


class MatchStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    RETIRED = "RETIRED"
    WALKOVER = "WALKOVER"
    DEFAULTED = "DEFAULTED"
    POSTPONED = "POSTPONED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    @property
    def has_winner(self) -> bool:
        return self in {
            MatchStatus.COMPLETED,
            MatchStatus.RETIRED,
            MatchStatus.WALKOVER,
            MatchStatus.DEFAULTED,
        }


class Handedness(StrEnum):
    RIGHT = "RIGHT"
    LEFT = "LEFT"
    UNKNOWN = "UNKNOWN"


class ResolutionDecision(StrEnum):
    AUTO_ACCEPT = "AUTO_ACCEPT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECT = "REJECT"


class ResolutionAction(StrEnum):
    """What an accepted decision does; review and reject decisions do nothing yet."""

    LINK_EXISTING = "LINK_EXISTING"
    CREATE_NEW = "CREATE_NEW"
    NONE = "NONE"


class EvidenceKind(StrEnum):
    STABLE_SOURCE_ID = "STABLE_SOURCE_ID"
    NAME_EXACT = "NAME_EXACT"
    NAME_INITIALS = "NAME_INITIALS"
    BIRTH_DATE = "BIRTH_DATE"
    NATIONALITY = "NATIONALITY"
    TOUR = "TOUR"
    OPPONENT_SCHEDULED_MATCH = "OPPONENT_SCHEDULED_MATCH"
    TOURNAMENT = "TOURNAMENT"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ReviewState(StrEnum):
    OPEN = "OPEN"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# Source-side records produced by approved-source parsers.


class SetScore(Contract):
    """Games per set in the record's participant order; tiebreak points when played."""

    games: tuple[NonNegative, NonNegative]
    tiebreak_points: tuple[NonNegative, NonNegative] | None = None


class ServeReturnCounts(Contract):
    """Raw counts; ``None`` is missing, never zero performance."""

    serve_points: NonNegative | None = None
    serve_points_won: NonNegative | None = None
    first_serves_in: NonNegative | None = None
    first_serve_points_won: NonNegative | None = None
    second_serve_points_won: NonNegative | None = None
    aces: NonNegative | None = None
    double_faults: NonNegative | None = None
    service_games: NonNegative | None = None
    break_points_faced: NonNegative | None = None
    break_points_saved: NonNegative | None = None
    return_points: NonNegative | None = None
    return_points_won: NonNegative | None = None

    @model_validator(mode="after")
    def denominators_hold(self) -> Self:
        pairs = (
            ("serve_points_won", "serve_points"),
            ("first_serves_in", "serve_points"),
            ("first_serve_points_won", "first_serves_in"),
            ("aces", "serve_points"),
            ("double_faults", "serve_points"),
            ("break_points_saved", "break_points_faced"),
            ("return_points_won", "return_points"),
        )
        for numerator, denominator in pairs:
            top, bottom = getattr(self, numerator), getattr(self, denominator)
            if top is not None and bottom is not None and top > bottom:
                raise ValueError(f"{numerator} exceeds its denominator {denominator}")
        played, first_in = self.serve_points, self.first_serves_in
        first_won, second_won = self.first_serve_points_won, self.second_serve_points_won
        if played is not None and first_in is not None and second_won is not None:
            if second_won > played - first_in:
                raise ValueError("second_serve_points_won exceeds second-serve points")
        if self.serve_points_won is not None and first_won is not None and second_won is not None:
            if first_won + second_won != self.serve_points_won:
                raise ValueError("First and second serve points won must sum to serve points won")
        return self


class SourcePlayerRecord(Contract):
    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    source_player_id: SourceKey
    full_name: SourceText
    tour: Tour | None = None
    birth_date: date | None = None
    nationality: CountryCode | None = None
    handedness: Handedness = Handedness.UNKNOWN


class SourceTournamentRecord(Contract):
    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    source_tournament_id: SourceKey
    name: SourceText
    season: Annotated[int, Field(ge=1968, le=2100, strict=True)]
    tour: Tour | None
    level: CompetitionLevel = CompetitionLevel.UNKNOWN
    surface: Surface = Surface.UNKNOWN
    environment: CourtEnvironment = CourtEnvironment.UNKNOWN
    timezone: SourceText | None = None
    start_date: date | None = None
    end_date: date | None = None


class SourceMatchRecord(Contract):
    """One provider's view of a match. Participant order is the provider's order."""

    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    source_match_id: SourceKey
    source_tournament_id: SourceKey
    season: Annotated[int, Field(ge=1968, le=2100, strict=True)]
    tour: Tour | None
    draw_type: DrawType
    draw_stage: DrawStage = DrawStage.UNKNOWN
    round: Round = Round.UNKNOWN
    best_of: BestOf = BestOf.UNKNOWN
    participant_ids: tuple[SourceKey, SourceKey]
    scheduled_start: Timestamp | None = None
    actual_start: Timestamp | None = None
    actual_end: Timestamp | None = None
    status: MatchStatus
    winner_id: SourceKey | None = None
    sets: tuple[SetScore, ...] = ()
    stats: tuple[ServeReturnCounts | None, ServeReturnCounts | None] = (None, None)


class SourceFormatRecord(Contract):
    """A source's statement of the deciding-set rule for one edition, stage and format."""

    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    source_tournament_id: SourceKey
    season: Annotated[int, Field(ge=1968, le=2100, strict=True)]
    draw_stage: DrawStage
    best_of: BestOf
    deciding_set: DecidingSetRule
    reference: SourceText


class SourceRankingRecord(Contract):
    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    source_player_id: SourceKey
    tour: Tour
    ranking_date: date
    rank: Annotated[int, Field(ge=1, strict=True)]
    points: NonNegative | None = None


# Canonical records.


class Player(Contract):
    schema_version: Literal["1.0"] = "1.0"
    player_id: UUID
    tour: Tour
    display_name: SourceText
    birth_date: date | None = None
    nationality: CountryCode | None = None
    handedness: Handedness = Handedness.UNKNOWN
    created_at: Timestamp


class PlayerAlias(Contract):
    """Versioned link from a source identity to a canonical player; never edited in place."""

    schema_version: Literal["1.0"] = "1.0"
    alias_id: UUID
    player_id: UUID
    source_id: Identifier
    source_player_id: SourceKey
    source_name: SourceText
    version: Annotated[int, Field(ge=1, strict=True)]
    active: bool
    decision: ResolutionDecision
    reviewed_by: Identifier | None = None
    evidence: tuple[EvidenceKind, ...]
    recorded_at: Timestamp
    supersedes: UUID | None = None

    @model_validator(mode="after")
    def decision_is_accepting(self) -> Self:
        if self.decision == ResolutionDecision.REJECT:
            raise ValueError("A rejected resolution cannot create an alias")
        if self.decision == ResolutionDecision.REVIEW_REQUIRED and self.reviewed_by is None:
            raise ValueError("A reviewed alias requires the reviewer identity")
        return self


class Tournament(Contract):
    schema_version: Literal["1.0"] = "1.0"
    tournament_id: UUID
    tour: Tour
    name: SourceText
    level: CompetitionLevel


class TournamentEdition(Contract):
    schema_version: Literal["1.0"] = "1.0"
    edition_id: UUID
    tournament_id: UUID
    season: int
    surface: Surface
    environment: CourtEnvironment
    timezone: SourceText | None
    start_date: date | None
    end_date: date | None


class Match(Contract):
    """Canonical match identity. Schedule, status and result are separate versions.

    ``player_ids`` uses one deterministic orientation (ascending UUID text) so a reversed
    source order cannot change identity or feature direction.
    """

    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    edition_id: UUID
    tour: Tour
    draw_type: DrawType
    draw_stage: DrawStage
    round: Round
    best_of: BestOf
    player_ids: tuple[UUID, UUID]
    created_at: Timestamp

    @model_validator(mode="after")
    def oriented_distinct_players(self) -> Self:
        first, second = self.player_ids
        if first == second:
            raise ValueError("A match requires two distinct players")
        if str(first) > str(second):
            raise ValueError("Canonical player order must be ascending by UUID text")
        return self


class MatchAlias(Contract):
    schema_version: Literal["1.0"] = "1.0"
    alias_id: UUID
    match_id: UUID
    source_id: Identifier
    source_match_id: SourceKey
    version: Annotated[int, Field(ge=1, strict=True)]
    active: bool
    decision: ResolutionDecision
    reviewed_by: Identifier | None = None
    swapped: bool
    recorded_at: Timestamp
    supersedes: UUID | None = None


class ScheduleVersion(Contract):
    match_id: UUID
    version: Annotated[int, Field(ge=1, strict=True)]
    scheduled_start: Timestamp | None
    source_id: Identifier
    availability: Availability


class StatusVersion(Contract):
    match_id: UUID
    version: Annotated[int, Field(ge=1, strict=True)]
    status: MatchStatus
    actual_start: Timestamp | None = None
    actual_end: Timestamp | None = None
    source_id: Identifier
    availability: Availability


class ResultVersion(Contract):
    """Result in canonical player order. A correction is a new version, never an edit."""

    match_id: UUID
    version: Annotated[int, Field(ge=1, strict=True)]
    status: MatchStatus
    winner_id: UUID
    sets: tuple[SetScore, ...]
    source_id: Identifier
    availability: Availability
    corrects_version: int | None = None


class StatsVersion(Contract):
    match_id: UUID
    player_id: UUID
    version: Annotated[int, Field(ge=1, strict=True)]
    counts: ServeReturnCounts
    source_id: Identifier
    availability: Availability


class EditionFormatVersion(Contract):
    """Deciding-set rule for one edition, draw stage and best-of format.

    A change or correction is a new version. The stage and best-of values must be known,
    because a rule for an unknown scope could attach to the wrong matches.
    """

    edition_id: UUID
    draw_stage: DrawStage
    best_of: BestOf
    version: Annotated[int, Field(ge=1, strict=True)]
    deciding_set: DecidingSetRule
    reference: SourceText
    source_id: Identifier
    availability: Availability
    corrects_version: int | None = None

    @model_validator(mode="after")
    def known_scope(self) -> Self:
        if self.draw_stage == DrawStage.UNKNOWN or self.best_of == BestOf.UNKNOWN:
            raise ValueError("A format rule needs a known draw stage and best-of format")
        if self.corrects_version is not None and not 1 <= self.corrects_version < self.version:
            raise ValueError("A correction must refer to an earlier version")
        return self


class RankingSnapshot(Contract):
    player_id: UUID
    tour: Tour
    ranking_date: date
    rank: Annotated[int, Field(ge=1, strict=True)]
    points: NonNegative | None
    source_id: Identifier
    availability: Availability


# Resolution requests and outcomes.


class EvidenceItem(Contract):
    kind: EvidenceKind
    detail: Annotated[str, Field(max_length=512)]


class Candidate(Contract):
    entity_id: UUID
    score: Probability
    decision: ResolutionDecision
    evidence: tuple[EvidenceItem, ...]
    conflicts: tuple[str, ...] = ()


class ResolutionContext(Contract):
    """Optional corroboration a caller knows about the record, e.g. from an event listing."""

    opponent_name: SourceText | None = None
    opponent_player_id: UUID | None = None
    scheduled_start: Timestamp | None = None
    tournament_name: SourceText | None = None


class PlayerResolution(Contract):
    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    source_player_id: SourceKey
    source_name: SourceText
    decision: ResolutionDecision
    action: ResolutionAction
    player_id: UUID | None
    candidates: tuple[Candidate, ...]
    reasons: tuple[str, ...]
    policy_version: Identifier
    resolved_at: Timestamp

    @model_validator(mode="after")
    def accepted_has_player(self) -> Self:
        accepted = self.decision == ResolutionDecision.AUTO_ACCEPT
        if accepted != (self.player_id is not None):
            raise ValueError("Only an accepted resolution names a player")
        if accepted == (self.action == ResolutionAction.NONE):
            raise ValueError("Accepted resolutions need an action; others must not have one")
        return self


class EventQuery(Contract):
    """A source event to map to a canonical match, e.g. a bookmaker listing (F05)."""

    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    source_event_id: SourceKey
    tour: Tour | None
    draw_type: DrawType = DrawType.SINGLES
    participants: tuple[SourcePlayerRecord, SourcePlayerRecord]
    scheduled_start: Timestamp | None
    tournament_name: SourceText | None = None


class MatchResolution(Contract):
    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    source_event_id: SourceKey
    decision: ResolutionDecision
    action: ResolutionAction
    match_id: UUID | None
    swapped: bool | None
    participants: tuple[PlayerResolution, PlayerResolution]
    candidates: tuple[Candidate, ...]
    reasons: tuple[str, ...]
    policy_version: Identifier
    resolved_at: Timestamp

    @model_validator(mode="after")
    def accepted_has_match(self) -> Self:
        accepted = self.decision == ResolutionDecision.AUTO_ACCEPT
        if accepted != (self.match_id is not None) or accepted != (self.swapped is not None):
            raise ValueError("Only an accepted match resolution names a match and orientation")
        return self

    @property
    def blocks_recommendations(self) -> bool:
        return self.decision != ResolutionDecision.AUTO_ACCEPT


class ResolutionPolicy(Contract):
    """Versioned thresholds and evidence weights. Initial values are candidates to validate."""

    version: Identifier
    auto_accept_threshold: Probability = Decimal("0.995")
    review_threshold: Probability = Decimal("0.90")
    weights: dict[EvidenceKind, Probability]
    schedule_window_hours: Annotated[int, Field(ge=1, le=240, strict=True)] = 36
    allow_create: frozenset[Identifier] = frozenset()

    @model_validator(mode="after")
    def thresholds_ordered(self) -> Self:
        if self.review_threshold >= self.auto_accept_threshold:
            raise ValueError("Review threshold must be below the auto-accept threshold")
        name_only = Decimal(1)
        for kind in (EvidenceKind.NAME_EXACT, EvidenceKind.NAME_INITIALS):
            name_only *= Decimal(1) - self.weights.get(kind, Decimal(0))
        if Decimal(1) - name_only >= self.review_threshold:
            raise ValueError("Name evidence alone must stay below the review threshold")
        return self


class IdentityResolver(Protocol):
    """Read-and-propose boundary shared with F05. It never merges ambiguous identities."""

    def resolve_player(
        self,
        record: SourcePlayerRecord,
        context: ResolutionContext | None = None,
        *,
        at: datetime,
    ) -> PlayerResolution: ...

    def resolve_event(self, query: EventQuery, *, at: datetime) -> MatchResolution: ...
