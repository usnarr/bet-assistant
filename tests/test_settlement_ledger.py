from datetime import timedelta
from decimal import Decimal
from threading import Thread
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from settlement_support import BET_TIME, MATCH_ID, PLAYER_A, pln

from tennis_engine.common.clock import FrozenClock
from tennis_engine.settlement.engine import PendingReason, SettlementResult, SettlementStatus
from tennis_engine.settlement.ledger import (
    EntryType,
    InMemoryLedgerStore,
    InsufficientFunds,
    LedgerConflict,
    VirtualBet,
    VirtualLedgerService,
)

RULE = "synthetic-book-settlement-v1"


def bet(index=1, stake="10.00", cash="25.00", **overrides):
    return VirtualBet.model_validate(
        {
            "bet_id": UUID(int=index),
            "ledger_id": "shadow",
            "decision_id": UUID(int=1000 + index),
            "bookmaker": "synthetic-book",
            "match_id": MATCH_ID,
            "selection_player_id": PLAYER_A,
            "decimal_odds": Decimal("2.50"),
            "stake": pln(stake),
            "cash_return_if_win": pln(cash),
            "payout_policy_version": "synthetic-policy-v1",
            "settlement_rule_version": RULE,
            "struck_at": BET_TIME,
        }
        | overrides
    )


def result(index=1, status=SettlementStatus.WON, stake="10.00", cash="25.00"):
    final = status != SettlementStatus.PENDING
    cash_value = {"WON": cash, "LOST": "0.00", "VOID": stake}.get(status.value)
    return SettlementResult(
        bet_id=UUID(int=index),
        status=status,
        rule_version=RULE,
        stake_deducted=pln(stake),
        cash_return=pln(cash_value) if final and cash_value else None,
        net_profit=pln(str(Decimal(cash_value) - Decimal(stake))) if final and cash_value else None,
        tax_amount=None,
        applied_rules=("fixture",),
        evidence=("raw:result",),
        pending_reason=None if final else PendingReason.RESULT_DISPUTED,
        outcome_observed_at=BET_TIME + timedelta(hours=3),
        settled_at=BET_TIME + timedelta(hours=4),
    )


@pytest.fixture
def ledger():
    service = VirtualLedgerService(InMemoryLedgerStore(), FrozenClock(BET_TIME))
    service.open_ledger("shadow", pln("100.00"))
    return service


def test_stake_and_settlement_are_idempotent(ledger):
    first = ledger.record_virtual_bet(bet())
    assert ledger.record_virtual_bet(bet()) == first
    assert first.amount == pln("-10.00") and first.balance_after == pln("90.00")
    settled = ledger.apply_settlement("shadow", result())
    assert ledger.apply_settlement("shadow", result()) == settled
    report = ledger.reconcile("shadow")
    assert report.closing_balance == pln("115.00") and report.entries == 3
    assert report.balanced and report.open_bets == 0 and report.settled_bets == 1


def test_reused_ids_and_mismatched_settlements_are_rejected(ledger):
    ledger.record_virtual_bet(bet())
    with pytest.raises(LedgerConflict, match="different terms"):
        ledger.record_virtual_bet(bet(stake="11.00"))
    with pytest.raises(LedgerConflict, match="unknown bet"):
        ledger.apply_settlement("shadow", result(index=2))
    with pytest.raises(LedgerConflict, match="stake"):
        ledger.apply_settlement("shadow", result(stake="9.00"))
    with pytest.raises(LedgerConflict, match="Unknown ledger"):
        ledger.record_virtual_bet(bet(index=3, ledger_id="other"))
    with pytest.raises(LedgerConflict, match="other terms"):
        ledger.open_ledger("shadow", pln("50.00"))


def test_stake_cannot_exceed_available_balance(ledger):
    ledger.record_virtual_bet(bet(stake="95.00", cash="200.00"))
    with pytest.raises(InsufficientFunds):
        ledger.record_virtual_bet(bet(index=2, stake="5.01"))


def test_pending_keeps_exposure_open_until_final(ledger):
    ledger.record_virtual_bet(bet())
    assert ledger.apply_settlement("shadow", result(status=SettlementStatus.PENDING)) == ()
    report = ledger.reconcile("shadow")
    assert report.open_exposure == pln("10.00") and report.pending_bets == 1
    ledger.apply_settlement("shadow", result(status=SettlementStatus.LOST))
    assert ledger.reconcile("shadow").open_exposure == pln("0.00")
    with pytest.raises(LedgerConflict, match="pending"):
        ledger.apply_settlement("shadow", result(status=SettlementStatus.PENDING))


def test_correction_appends_reversal_and_replacement(ledger):
    ledger.record_virtual_bet(bet())
    won = ledger.apply_settlement("shadow", result())
    with pytest.raises(LedgerConflict, match="correction"):
        ledger.apply_settlement("shadow", result(status=SettlementStatus.VOID))
    corrected = ledger.apply_settlement(
        "shadow", result(status=SettlementStatus.VOID), correction_reason="Official result fix"
    )
    reversal, replacement = corrected
    assert reversal.entry_type == EntryType.REVERSAL
    assert reversal.reverses_entry_id == won[0].entry_id and reversal.amount == pln("-25.00")
    assert replacement.amount == pln("10.00") and replacement.balance_after == pln("100.00")
    # Retrying the correction has no second effect; a second correction back is allowed.
    assert ledger.apply_settlement(
        "shadow", result(status=SettlementStatus.VOID), correction_reason="Official result fix"
    ) == (replacement,)
    back = ledger.apply_settlement("shadow", result(), correction_reason="Appeal upheld")
    assert back[-1].balance_after == pln("115.00")
    report = ledger.reconcile("shadow")
    assert report.balanced and report.entries == 7 and report.settled_bets == 1


def test_virtual_bet_contract_is_explicitly_virtual():
    with pytest.raises(ValidationError):
        bet(virtual=False)
    with pytest.raises(ValidationError):
        bet(stake="0.00")


def test_concurrent_stakes_never_overspend():
    service = VirtualLedgerService(InMemoryLedgerStore(), FrozenClock(BET_TIME))
    service.open_ledger("shadow", pln("100.00"))
    failures: list[Exception] = []

    def attempt(index: int) -> None:
        try:
            service.record_virtual_bet(bet(index=index, stake="30.00", cash="60.00"))
        except InsufficientFunds as error:
            failures.append(error)

    threads = [Thread(target=attempt, args=(index,)) for index in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    report = service.reconcile("shadow")
    assert len(failures) == 5 and report.open_exposure == pln("90.00")
    assert report.closing_balance == pln("10.00") and report.balanced


statuses = st.sampled_from([SettlementStatus.WON, SettlementStatus.LOST, SettlementStatus.VOID])


@settings(max_examples=60, deadline=None)
@given(
    stakes=st.lists(st.integers(min_value=1, max_value=2000), min_size=1, max_size=8),
    outcomes=st.lists(st.tuples(statuses, statuses, st.booleans()), min_size=8, max_size=8),
)
def test_property_ledger_reconciles_under_retries_and_corrections(stakes, outcomes):
    service = VirtualLedgerService(InMemoryLedgerStore(), FrozenClock(BET_TIME))
    service.open_ledger("shadow", pln("200.00"))
    expected = Decimal("200.00")
    for index, cents in enumerate(stakes, start=1):
        stake = (Decimal(cents) / 100).quantize(Decimal("0.01"))
        cash = (stake * Decimal("2.5")).quantize(Decimal("0.01"))
        position = bet(index=index, stake=str(stake), cash=str(cash))
        service.record_virtual_bet(position)
        service.record_virtual_bet(position)  # Retry.
        first, final, correct = outcomes[index - 1]
        service.apply_settlement("shadow", result(index, first, str(stake), str(cash)))
        service.apply_settlement("shadow", result(index, first, str(stake), str(cash)))
        chosen = first
        if correct and final != first:
            service.apply_settlement(
                "shadow", result(index, final, str(stake), str(cash)), correction_reason="fix"
            )
            chosen = final
        credit = {"WON": cash, "LOST": Decimal("0.00"), "VOID": stake}[chosen.value]
        expected += credit - stake
    report = service.reconcile("shadow")
    assert report.balanced and report.open_bets == 0
    assert report.closing_balance == pln(str(expected))
