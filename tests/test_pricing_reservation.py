from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from settlement_support import BET_TIME, MATCH_ID, PLAYER_A, PLAYER_B, pln

from tennis_engine.common.clock import FrozenClock
from tennis_engine.pricing.reservation import (
    ExposureService,
    MemoryReservationStore,
    ReservationConflict,
    ReservationRejected,
    ReservationRequest,
)
from tennis_engine.settlement.engine import SettlementResult, SettlementStatus
from tennis_engine.settlement.ledger import InMemoryLedgerStore, VirtualBet, VirtualLedgerService

OTHER_MATCH = UUID(int=4242)
EVENT_CAP = Decimal("30.00")


def setup():
    clock = FrozenClock(BET_TIME)
    ledger = VirtualLedgerService(InMemoryLedgerStore(), clock)
    ledger.open_ledger("shadow", pln("100.00"))
    return clock, ledger, ExposureService(MemoryReservationStore(), ledger, clock)


def request(key, stake="10.00", match=MATCH_ID, selection=PLAYER_A, book="synthetic-book"):
    return ReservationRequest(
        decision_key=key,
        ledger_id="shadow",
        match_id=match,
        bookmaker=book,
        selection_player_id=selection,
        stake=pln(stake),
        ttl_seconds=120,
    )


def event_cap(state):
    return min(EVENT_CAP - state.event_exposure, state.bankroll)


def bet_for(reservation, **overrides):
    return VirtualBet.model_validate(
        {
            "bet_id": reservation.reservation_id,
            "ledger_id": "shadow",
            "decision_id": UUID(int=9),
            "bookmaker": reservation.bookmaker,
            "match_id": reservation.match_id,
            "selection_player_id": reservation.selection_player_id,
            "decimal_odds": Decimal("2.00"),
            "stake": reservation.stake,
            "cash_return_if_win": pln(str(reservation.stake.amount * 2)),
            "payout_policy_version": "synthetic-policy-v1",
            "settlement_rule_version": "synthetic-book-settlement-v1",
            "struck_at": reservation.reserved_at,
        }
        | overrides
    )


def test_concurrent_reservations_never_exceed_the_cap():
    _, _, service = setup()
    rejected = []

    def attempt(index):
        try:
            service.reserve(request(f"decision-{index}"), event_cap)
        except ReservationRejected as error:
            rejected.append(error)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(10)))
    state = service.exposure("shadow", match_id=MATCH_ID, bookmaker="synthetic-book")
    assert state.event_exposure == EVENT_CAP and len(rejected) == 7
    assert state.bankroll == Decimal("70.00") and state.open_bets == 3


def test_retries_return_the_same_reservation_and_reuse_with_other_terms_fails():
    _, _, service = setup()
    first = service.reserve(request("decision-1"), event_cap)
    assert service.reserve(request("decision-1"), event_cap) == first
    with pytest.raises(ReservationConflict):
        service.reserve(request("decision-1", stake="11.00"), event_cap)
    state = service.exposure("shadow", match_id=MATCH_ID, bookmaker="synthetic-book")
    assert state.event_exposure == Decimal("10.00")


def test_opposite_selections_and_bookmakers_share_the_event_exposure():
    _, _, service = setup()
    service.reserve(request("a", stake="20.00", selection=PLAYER_A), event_cap)
    with pytest.raises(ReservationRejected):
        service.reserve(request("b", stake="20.00", selection=PLAYER_B, book="book-2"), event_cap)
    service.reserve(request("c", stake="20.00", match=OTHER_MATCH), event_cap)


def test_expiry_releases_capacity_and_blocks_a_late_commit():
    clock, _, service = setup()
    held = service.reserve(request("decision-1", stake="30.00"), event_cap)
    with pytest.raises(ReservationRejected):
        service.reserve(request("decision-2"), event_cap)
    clock.advance(timedelta(seconds=120))
    service.reserve(request("decision-2"), event_cap)
    with pytest.raises(ReservationConflict, match="no longer active"):
        service.commit(held.reservation_id, bet_for(held))


def test_commit_counts_the_stake_once_and_release_frees_it():
    _, ledger, service = setup()
    held = service.reserve(request("decision-1", stake="25.00"), event_cap)
    entry = service.commit(held.reservation_id, bet_for(held))
    assert service.commit(held.reservation_id, bet_for(held)) == entry  # Retry.
    state = service.exposure("shadow", match_id=MATCH_ID, bookmaker="synthetic-book")
    assert state.event_exposure == Decimal("25.00") and state.bankroll == Decimal("75.00")
    assert state.equity == Decimal("100.00") and state.peak_bankroll == Decimal("100.00")
    other = service.reserve(request("decision-2", stake="5.00"), event_cap)
    service.release("shadow", other.reservation_id, "quote expired before publication")
    with pytest.raises(ReservationConflict, match="no longer active"):
        service.commit(other.reservation_id, bet_for(other))
    with pytest.raises(ReservationConflict, match="committed"):
        service.release("shadow", held.reservation_id, "late")
    assert ledger.reconcile("shadow").balanced


def test_commit_must_match_the_reservation():
    _, _, service = setup()
    held = service.reserve(request("decision-1"), event_cap)
    with pytest.raises(ReservationConflict):
        service.commit(held.reservation_id, bet_for(held, stake=pln("11.00")))
    with pytest.raises(ReservationConflict):
        service.commit(held.reservation_id, bet_for(held, match_id=OTHER_MATCH))


def test_settlement_updates_exposure_and_a_loss_lowers_equity():
    clock, ledger, service = setup()
    held = service.reserve(request("decision-1", stake="20.00"), event_cap)
    service.commit(held.reservation_id, bet_for(held))
    clock.advance(timedelta(hours=5))
    ledger.apply_settlement(
        "shadow",
        SettlementResult(
            bet_id=held.reservation_id,
            status=SettlementStatus.LOST,
            rule_version="synthetic-book-settlement-v1",
            stake_deducted=pln("20.00"),
            cash_return=pln("0.00"),
            net_profit=pln("-20.00"),
            tax_amount=None,
            applied_rules=("fixture",),
            evidence=("raw:result",),
            outcome_observed_at=clock.now(),
            settled_at=clock.now(),
        ),
    )
    state = service.exposure("shadow", match_id=MATCH_ID, bookmaker="synthetic-book")
    assert state.event_exposure == 0 and state.equity == Decimal("80.00")
    assert state.peak_bankroll == Decimal("100.00") and state.bankroll == Decimal("80.00")
