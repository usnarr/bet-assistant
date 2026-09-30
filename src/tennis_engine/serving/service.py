"""F14 read service: filters, pagination, read-time rechecks and view building.

The service only reads. A read-time failure changes the served view to not actionable; it
never rewrites the stored record. Audit reads return the stored record unchanged.
"""

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from tennis_engine.common.clock import Clock, require_aware
from tennis_engine.common.contracts import Money
from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.governance.contracts import Principal

from .auth import Permission, allowed
from .checks import ReadChecks, RedistributionPolicy, source_health
from .contracts import (
    AuditView,
    ComparisonRow,
    FormatView,
    MatchAnalysis,
    Mode,
    QuotePoint,
    RecommendationPage,
    RecommendationView,
    ResponsibleUseStatus,
    SourceHealth,
    SourceRights,
    StoredDecision,
)
from .explain import NO_PLACEMENT, explain, reasons
from .store import DecisionQuery, DecisionStore

if TYPE_CHECKING:
    from tennis_engine.monitoring.instruments import ServingMetrics

DEFAULT_BOOKMAKERS = frozenset({"betclic", "superbet", "fortuna"})
MARKET_ALIASES = {
    "match_winner": "TENNIS_MATCH_WINNER",
    "TENNIS_MATCH_WINNER": "TENNIS_MATCH_WINNER",
}
MAX_LIMIT = 100
# Rows one list request may read and recheck. A page can then be short with a cursor.
MAX_SCAN = 1000
# Recorded decisions that can be served as each effective decision. A read-time hard stop
# turns BET and WATCH into NO_BET; it never raises a decision.
SERVED_FROM = {
    RecommendationStatus.BET: frozenset({RecommendationStatus.BET}),
    RecommendationStatus.WATCH: frozenset({RecommendationStatus.WATCH}),
    RecommendationStatus.NO_BET: frozenset(RecommendationStatus),
}
ZERO = Money(amount=Decimal("0.00"))


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RecommendationFilter:
    view: Literal["current", "history"] = "current"
    bookmaker: str | None = None
    market: str | None = None
    # Effective (served) decision, after read-time checks. This is the `decision` field.
    statuses: frozenset[RecommendationStatus] = frozenset()
    # Stored decision. This is the `recorded_decision` field.
    recorded_statuses: frozenset[RecommendationStatus] = frozenset()
    starts_after: datetime | None = None
    starts_before: datetime | None = None
    limit: int = 50
    cursor: str | None = None

    def store_statuses(self) -> frozenset[RecommendationStatus] | None:
        """Recorded decisions to read. None means that no record can match."""
        allowed = frozenset(RecommendationStatus)
        if self.statuses:
            allowed = frozenset().union(*(SERVED_FROM[status] for status in self.statuses))
        if self.recorded_statuses:
            allowed &= self.recorded_statuses
        if not allowed:
            return None
        return frozenset() if allowed == frozenset(RecommendationStatus) else allowed

    def fingerprint(self) -> str:
        parts = [
            self.view,
            self.bookmaker or "",
            self.market or "",
            ",".join(sorted(self.statuses)),
            ",".join(sorted(self.recorded_statuses)),
            self.starts_after.isoformat() if self.starts_after else "",
            self.starts_before.isoformat() if self.starts_before else "",
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def encode_cursor(key: tuple[datetime, UUID], fingerprint: str) -> str:
    payload = {"s": key[0].isoformat(), "d": str(key[1]), "f": fingerprint}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, fingerprint: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        key = (require_aware(datetime.fromisoformat(payload["s"])), UUID(payload["d"]))
        matches = payload["f"] == fingerprint
    except (binascii.Error, ValueError, KeyError, TypeError) as error:
        raise ApiError(422, "INVALID_CURSOR", "The cursor is not valid.") from error
    if not matches:
        raise ApiError(422, "INVALID_CURSOR", "The cursor belongs to other filters.")
    return key


@dataclass
class RecommendationService:
    store: DecisionStore
    checks: ReadChecks
    clock: Clock
    redistribution: RedistributionPolicy
    account_scope: str = "shadow"
    bookmakers: frozenset[str] = field(default_factory=lambda: DEFAULT_BOOKMAKERS)
    stale_after_seconds: int = 300
    # F15.3 counters. None means no metrics are recorded.
    metrics: "ServingMetrics | None" = None

    # Filters -----------------------------------------------------------------------

    def _validate(self, request: RecommendationFilter) -> None:
        if request.bookmaker is not None and request.bookmaker not in self.bookmakers:
            raise ApiError(422, "UNSUPPORTED_BOOKMAKER", "The bookmaker is not supported.")
        if request.market is not None and request.market not in MARKET_ALIASES:
            raise ApiError(422, "UNSUPPORTED_MARKET", "Only the match-winner market exists.")
        for name, value in (
            ("starts_after", request.starts_after),
            ("starts_before", request.starts_before),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ApiError(422, "INVALID_FILTER", f"{name} needs a timezone offset.")
        if (
            request.starts_after is not None
            and request.starts_before is not None
            and request.starts_after >= request.starts_before
        ):
            raise ApiError(422, "INVALID_FILTER", "starts_after must be before starts_before.")
        if not 1 <= request.limit <= MAX_LIMIT:
            raise ApiError(422, "INVALID_FILTER", f"limit must be from 1 to {MAX_LIMIT}.")

    # Views -------------------------------------------------------------------------

    def _withhold(self, principal: Principal, now: datetime) -> Callable[[str], bool]:
        if allowed(principal, Permission.READ_RESTRICTED_SOURCE_VALUES):
            return lambda source_id: False
        cache: dict[str, bool] = {}

        def withhold(source_id: str) -> bool:
            if source_id not in cache:
                try:
                    cache[source_id] = not self.redistribution.allows(source_id, now)
                except Exception:  # noqa: BLE001 - an unknown right is no right
                    cache[source_id] = True
            return cache[source_id]

        return withhold

    def _read_reasons(self, stored: StoredDecision, now: datetime, current: bool) -> list[str]:
        found: list[str] = []
        if now >= stored.record.expires_at:
            found.append("DECISION_EXPIRED")
        if self.store.successors(stored.record.decision_id):
            found.append("SUPERSEDED")
        if current and not found:
            try:
                found.extend(self.checks.check(stored, now))
            except Exception:  # noqa: BLE001 - a failed check must fail closed
                found.append("READ_CHECK_UNAVAILABLE")
        if not current:
            found.append("HISTORY_VIEW")
        return found

    def view(
        self,
        stored: StoredDecision,
        now: datetime,
        withhold: Callable[[str], bool],
        *,
        current: bool,
    ) -> RecommendationView:
        record, context = stored.record, stored.context
        read_reasons = self._read_reasons(stored, now, current)
        blocked = bool(read_reasons)
        status = record.status
        # A read-time hard stop turns BET and WATCH into NO_BET, but only in current views.
        if current and blocked and status != RecommendationStatus.NO_BET:
            status = RecommendationStatus.NO_BET
        actionable = status == RecommendationStatus.BET and not blocked
        if current and self.metrics is not None:
            self.metrics.record_view(record.status, status, read_reasons)
        match = context.match
        selected = match.player(record.selection_player_id)
        odds_withheld = context.quote_source_id is not None and withhold(context.quote_source_id)
        value = record.value
        room = record.capacity
        age = None
        if context.quote_observed_at is not None:
            age = int((now - context.quote_observed_at).total_seconds())
        return RecommendationView(
            recommendation_id=record.decision_id,
            version=record.version,
            supersedes=record.supersedes,
            decision_key=record.decision_key,
            match_id=match.match_id,
            event=f"{match.players[0].display_name} vs {match.players[1].display_name}",
            tournament=match.tournament_name,
            format=FormatView(tour=match.tour, draw_type=match.draw_type, best_of=match.best_of),
            scheduled_start=match.scheduled_start,
            bookmaker=record.bookmaker,
            market=context.market,
            selection_player_id=record.selection_player_id,
            selection=selected.display_name if selected else None,
            displayed_odds=None if odds_withheld else record.decimal_odds,
            odds_withheld=odds_withheld,
            recorded_decision=record.status,
            decision=status,
            actionable=actionable,
            recommended_stake=record.stake if actionable else ZERO,
            recorded_stake=record.stake,
            maximum_stake=Money(amount=room.maximum_stake) if room is not None else None,
            cash_return_if_win=value.cash_return_if_win if value else None,
            probability=record.central_probability,
            probability_low=record.conservative_probability,
            probability_semantics=record.probability_semantics,
            break_even_probability=value.break_even_probability if value else None,
            expected_value=value.expected_value if value else None,
            expected_roi=value.expected_roi if value else None,
            conservative_expected_value=value.conservative_expected_value if value else None,
            conservative_roi=value.conservative_roi if value else None,
            probability_edge=value.probability_edge if value else None,
            generated_at=record.decided_at,
            quote_observed_at=context.quote_observed_at,
            quote_age_seconds=age,
            expires_at=record.expires_at,
            expired=now >= record.expires_at,
            superseded="SUPERSEDED" in read_reasons,
            failed_gates=tuple(gate.value for gate in record.failed_gates),
            reasons=reasons(stored),
            read_time_reasons=tuple(read_reasons),
            policy_versions=record.policy_versions,
            payout_source=record.payout_source,
            mode=context.mode,
        )

    def responsible_use(self, now: datetime) -> ResponsibleUseStatus:
        try:
            return self.checks.responsible_use(self.account_scope, now)
        except Exception:  # noqa: BLE001 - a failed check must fail closed
            return ResponsibleUseStatus(
                account_scope=self.account_scope,
                allowed=False,
                reason="CHECK_UNAVAILABLE",
                policy_version=None,
            )

    def source_health(self, now: datetime) -> tuple[SourceHealth, ...]:
        return source_health(
            self.checks, self.store.latest_observations(), now, self.stale_after_seconds
        )

    # Routes ------------------------------------------------------------------------

    def source_health_for(self, principal: Principal) -> tuple[SourceHealth, ...]:
        if not allowed(principal, Permission.READ_RECOMMENDATIONS):
            raise ApiError(403, "PERMISSION_DENIED", "This role cannot read source health.")
        return self.source_health(require_aware(self.clock.now()))

    def recommendations(
        self, principal: Principal, request: RecommendationFilter
    ) -> RecommendationPage:
        if not allowed(principal, Permission.READ_RECOMMENDATIONS):
            raise ApiError(403, "PERMISSION_DENIED", "This role cannot read recommendations.")
        self._validate(request)
        now = require_aware(self.clock.now())
        fingerprint = request.fingerprint()
        after = decode_cursor(request.cursor, fingerprint) if request.cursor else None
        current = request.view == "current"
        withhold = self._withhold(principal, now)
        views: list[RecommendationView] = []
        next_key: tuple[datetime, UUID] | None = None
        store_statuses = request.store_statuses()
        scanned, exhausted = 0, store_statuses is None
        while not exhausted and len(views) <= request.limit and scanned < MAX_SCAN:
            batch = request.limit + 1
            items = self.store.query(
                DecisionQuery(
                    limit=batch,
                    bookmaker=request.bookmaker,
                    statuses=store_statuses or frozenset(),
                    starts_after=request.starts_after,
                    starts_before=request.starts_before,
                    active_at=now if current else None,
                    latest_only=current,
                    after=after,
                )
            )
            exhausted = len(items) < batch
            for item in items:
                scanned += 1
                after = (item.scheduled_start, item.record.decision_id)
                # The filter uses the served view, so every candidate is rechecked now.
                view = self.view(item, now, withhold, current=current)
                if not request.statuses or view.decision in request.statuses:
                    views.append(view)
                if len(views) > request.limit or scanned >= MAX_SCAN:
                    exhausted = exhausted and item is items[-1]
                    break
        if len(views) > request.limit:
            views = views[: request.limit]
            next_key = (views[-1].scheduled_start, views[-1].recommendation_id)
        elif not exhausted and after is not None:
            # The scan limit stopped the read. Continue after the last row read.
            next_key = after
        next_cursor = encode_cursor(next_key, fingerprint) if next_key else None
        return RecommendationPage(
            generated_at=now,
            view=request.view,
            mode=Mode.SHADOW,
            responsible_use=self.responsible_use(now),
            notice=f"Shadow mode. All records are virtual. {NO_PLACEMENT}",
            recommendations=tuple(views),
            next_cursor=next_cursor,
        )

    def analysis(self, principal: Principal, match_id: UUID) -> MatchAnalysis:
        if not allowed(principal, Permission.READ_ANALYSIS):
            raise ApiError(403, "PERMISSION_DENIED", "This role cannot read match analysis.")
        now = require_aware(self.clock.now())
        items = list(
            self.store.query(DecisionQuery(limit=10_000, match_id=match_id, latest_only=False))
        )
        if not items:
            raise ApiError(404, "MATCH_NOT_FOUND", "No decision exists for this match.")
        withhold = self._withhold(principal, now)
        items.sort(key=lambda item: (item.record.decided_at, item.record.decision_id))
        latest_items = [
            item for item in items if not self.store.successors(item.record.decision_id)
        ]
        newest = items[-1]
        match = newest.context.match
        views = tuple(self.view(item, now, withhold, current=True) for item in latest_items)
        return MatchAnalysis(
            generated_at=now,
            match=match,
            mode=Mode.SHADOW,
            comparison=_comparison(newest, withhold),
            components=newest.context.components,
            recommendations=views,
            decision_quotes=tuple(
                QuotePoint(
                    bookmaker=item.record.bookmaker,
                    selection_player_id=item.record.selection_player_id,
                    decimal_odds=None if hidden else item.record.decimal_odds,
                    odds_withheld=hidden,
                    observed_at=item.context.quote_observed_at,
                    recommendation_id=item.record.decision_id,
                )
                for item in items
                if item.record.bookmaker is not None
                and item.record.selection_player_id is not None
                and item.context.quote_observed_at is not None
                and item.context.quote_source_id is not None
                for hidden in (withhold(item.context.quote_source_id),)
            ),
            explanations={
                str(item.record.decision_id): explain(item, withhold) for item in latest_items
            },
            source_health=self.source_health(now),
        )

    def audit(self, principal: Principal, recommendation_id: UUID) -> AuditView:
        if not allowed(principal, Permission.READ_AUDIT):
            raise ApiError(403, "PERMISSION_DENIED", "This role cannot read audit records.")
        stored = self.store.get(recommendation_id)
        if stored is None:
            raise ApiError(404, "RECOMMENDATION_NOT_FOUND", "The recommendation does not exist.")
        now = require_aware(self.clock.now())
        chain: list[UUID] = [stored.record.decision_id]
        cursor = stored
        while cursor.record.supersedes is not None:
            previous = self.store.get(cursor.record.supersedes)
            if previous is None:
                break
            chain.insert(0, previous.record.decision_id)
            cursor = previous
        rights = []
        for source_id in stored.context.source_ids:
            try:
                permitted = self.redistribution.allows(source_id, now)
            except Exception:  # noqa: BLE001 - an unknown right is no right
                permitted = False
            rights.append(SourceRights(source_id=source_id, redistribution_allowed=permitted))
        return AuditView(
            generated_at=now,
            record=stored.record,
            context=stored.context,
            version_chain=tuple(chain),
            superseded_by=tuple(self.store.successors(recommendation_id)),
            source_rights=tuple(rights),
        )


def _comparison(
    stored: StoredDecision, withhold: Callable[[str], bool]
) -> tuple[ComparisonRow, ...]:
    """Facts in canonical player order: first value is players[0], second is players[1]."""
    match = stored.context.match
    swap = stored.record.selection_player_id == match.players[1].player_id
    rows = []
    for fact in stored.context.facts:
        oriented = fact.swapped() if swap else fact
        hidden = withhold(fact.source_id)
        rows.append(
            ComparisonRow(
                key=fact.key,
                label=fact.label,
                kind="WITHHELD" if hidden else fact.kind,
                unit=fact.unit,
                first_value=None if hidden else oriented.selection_value,
                second_value=None if hidden else oriented.opponent_value,
                difference=None if hidden else oriented.difference,
                as_of=fact.as_of,
                source_id=fact.source_id,
            )
        )
    return tuple(rows)
