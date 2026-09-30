from datetime import timedelta
from decimal import Decimal

from pricing_support import decision_policy, responsible
from settlement_support import PLAYER_A, pln
from test_pricing_decision import AT, RULES, SHA, fresh, inputs

from tennis_engine.common.clock import FrozenClock
from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.governance.contracts import Decision
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.ingestion.bookmakers.contracts import EventState, QuoteState, RawQuote
from tennis_engine.ingestion.bookmakers.quotes import QuoteObservation
from tennis_engine.pricing.decision import Gate, decide, prepare_publication
from tennis_engine.pricing.reservation import (
    ExposureService,
    MemoryReservationStore,
    ReservationRequest,
)
from tennis_engine.settlement.ledger import InMemoryLedgerStore, VirtualLedgerService

ALLOWED = Decision(allowed=True, version="fixture", revision=1)
RESPONSIBLE = PolicyLookup(ALLOWED, responsible())


def observation(odds="2.30"):
    return QuoteObservation(
        quote=RawQuote(
            bookmaker="synthetic-book",
            source_event_id="e-1",
            source_market_id="m-1",
            source_selection_id="s-1",
            market_label="Winner",
            market="TENNIS_MATCH_WINNER",
            selection_label="A",
            participant_index=0,
            line=None,
            decimal_odds=Decimal(odds),
            state=QuoteState.OPEN,
        ),
        observed_at=AT - timedelta(seconds=10),
        parser_version="synthetic-book-v1",
        raw_content_sha256=SHA,
        scheduled_start=AT + timedelta(hours=3),
        event_state=EventState.PRE_MATCH,
    )


def exposure_service():
    clock = FrozenClock(AT + timedelta(seconds=5))
    ledger = VirtualLedgerService(InMemoryLedgerStore(), clock)
    ledger.open_ledger("shadow", pln("1000.00"))
    return ExposureService(MemoryReservationStore(), ledger, clock)


def reserver(service, record):
    def reserve(stake, allowed_stake):
        return service.reserve(
            ReservationRequest(
                decision_key=record.decision_key,
                ledger_id="shadow",
                match_id=record.match_id,
                bookmaker=record.bookmaker,
                selection_player_id=PLAYER_A,
                stake=pln(str(stake)),
                ttl_seconds=120,
            ),
            allowed_stake,
        ).reservation_id

    return reserve


def publish(record, service, **overrides):
    arguments = {
        "now": AT + timedelta(seconds=5),
        "publication": ALLOWED,
        "actionability": fresh(observation=observation()),
        "responsible_use": RESPONSIBLE,
        "policy": decision_policy(),
        "rules": RULES,
        "reserve": reserver(service, record),
    } | overrides
    return prepare_publication(record, **arguments)


def test_bet_is_published_with_a_reservation():
    record = decide(inputs())
    service = exposure_service()
    outcome = publish(record, service)
    assert outcome.published and outcome.reservation_id is not None
    state = service.exposure("shadow", match_id=record.match_id, bookmaker="synthetic-book")
    assert state.event_exposure == record.stake.amount


def test_kill_switch_during_computation_prevents_publication():
    record = decide(inputs())
    outcome = publish(record, exposure_service(), publication=Decision.deny("SOURCE_DISABLED"))
    assert not outcome.published
    assert outcome.record.status == RecommendationStatus.NO_BET
    assert outcome.record.supersedes == record.decision_id and outcome.record.version == 2
    assert outcome.record.stake.amount == 0 and Gate.RULES in outcome.record.failed_gates
    assert record.status == RecommendationStatus.BET  # The original stays auditable.


def test_expired_cached_decision_is_not_served():
    record = decide(inputs())
    outcome = publish(record, exposure_service(), now=record.expires_at)
    assert not outcome.published and outcome.reasons == ("DECISION_EXPIRED",)
    watch = decide(
        inputs(
            model=inputs().model.model_copy(update={"conservative_probability": Decimal("0.47")})
        )
    )
    later = publish(watch, exposure_service(), now=watch.expires_at + timedelta(seconds=1))
    assert not later.published


def test_price_change_and_responsible_use_stop_are_rechecked():
    record = decide(inputs())
    moved = publish(
        record, exposure_service(), actionability=fresh(observation=observation("2.20"))
    )
    assert not moved.published and "RECHECK:PRICE_CHANGED" in moved.reasons
    stopped = publish(
        record, exposure_service(), responsible_use=PolicyLookup(Decision.deny("COOLING_OFF"))
    )
    assert not stopped.published and Gate.RESPONSIBLE_USE in stopped.record.failed_gates


def test_second_decision_on_the_same_event_cannot_overspend():
    service = exposure_service()
    first = decide(inputs())
    second = decide(inputs(decision_key="decision-2"))
    assert publish(first, service).published
    outcome = publish(second, service)
    # The event cap is PLN 50.00; two stakes of this size do not fit.
    assert first.stake.amount + second.stake.amount > Decimal("50.00")
    assert not outcome.published
    assert Gate.RISK_BUDGET in outcome.record.failed_gates
    assert outcome.record.stake.amount == 0
    state = service.exposure("shadow", match_id=first.match_id, bookmaker="synthetic-book")
    assert state.event_exposure <= Decimal("50.00")
