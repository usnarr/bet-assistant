"""F06.6 append-only virtual bankroll ledger.

Entries are never updated or deleted. A settlement has one financial effect per bet; a
corrected settlement appends a reversal plus a replacement. Virtual and actual ledgers
are distinct; this service records only virtual (shadow) activity and never places bets.
"""

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from decimal import Decimal
from enum import StrEnum
from threading import Lock
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.clock import Clock
from tennis_engine.common.contracts import (
    Contract,
    Currency,
    ExactDecimal,
    Identifier,
    Money,
    Timestamp,
)
from tennis_engine.common.ids import stable_id

from .engine import SettlementResult, SettlementStatus


class LedgerScope(StrEnum):
    VIRTUAL = "VIRTUAL"
    ACTUAL = "ACTUAL"  # Reserved for later manually recorded activity; never automated.


class EntryType(StrEnum):
    OPENING_BALANCE = "OPENING_BALANCE"
    STAKE_DEBIT = "STAKE_DEBIT"
    SETTLEMENT_CREDIT = "SETTLEMENT_CREDIT"
    REVERSAL = "REVERSAL"


class LedgerConflict(ValueError):
    """The same idempotency key or bet was reused with different content."""


class InsufficientFunds(ValueError):
    pass


class LedgerAccount(Contract):
    ledger_id: Identifier
    scope: LedgerScope
    currency: Currency = Currency.PLN
    opening_balance: Money
    created_at: Timestamp

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.opening_balance.amount < 0 or self.opening_balance.currency != self.currency:
            raise ValueError("The opening balance is non-negative in the ledger currency")
        return self


class VirtualBet(Contract):
    """A shadow position recorded from a BET decision. It is not a wager."""

    bet_id: UUID
    ledger_id: Identifier
    decision_id: UUID
    bookmaker: Identifier
    match_id: UUID
    selection_player_id: UUID
    decimal_odds: Annotated[ExactDecimal, Field(gt=1)]
    stake: Money
    cash_return_if_win: Money
    payout_policy_version: Identifier
    settlement_rule_version: Identifier
    struck_at: Timestamp
    virtual: Literal[True] = True

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.stake.amount <= 0 or self.cash_return_if_win.currency != self.stake.currency:
            raise ValueError("A virtual bet needs a positive stake in one currency")
        return self


class LedgerEntry(Contract):
    entry_id: UUID
    ledger_id: Identifier
    sequence: Annotated[int, Field(ge=1, strict=True)]
    entry_type: EntryType
    idempotency_key: Annotated[str, Field(min_length=1, max_length=300)]
    amount: Money  # Signed delta.
    balance_after: Money
    bet_id: UUID | None = None
    settlement_digest: str | None = None
    settlement_status: SettlementStatus | None = None
    reverses_entry_id: UUID | None = None
    reason: Annotated[str, Field(min_length=1)]
    recorded_at: Timestamp


class SettlementRecord(Contract):
    """Every settlement decision, including PENDING, kept for audit."""

    record_id: UUID
    bet_id: UUID
    digest: str
    result: SettlementResult
    recorded_at: Timestamp


class LedgerReconciliation(Contract):
    ledger_id: Identifier
    opening_balance: Money
    total_deltas: Money
    closing_balance: Money
    open_exposure: Money
    open_bets: int
    settled_bets: int
    pending_bets: int
    entries: int
    balanced: bool


class LedgerView(Contract):
    account: LedgerAccount
    entries: tuple[LedgerEntry, ...]
    bets: tuple[VirtualBet, ...]
    open_bet_ids: frozenset[UUID]

    @property
    def balance(self) -> Decimal:
        return self.entries[-1].balance_after.amount if self.entries else Decimal("0.00")

    def equity_history(self) -> tuple[Decimal, ...]:
        """Cash plus open stakes at cost after each entry."""
        stakes = {bet.bet_id: bet.stake.amount for bet in self.bets}
        open_stakes: dict[UUID, Decimal] = {}
        history = []
        for entry in self.entries:
            bet_id = entry.bet_id
            if bet_id is None:
                pass
            elif entry.entry_type in (EntryType.STAKE_DEBIT, EntryType.REVERSAL):
                # A reversal reopens the bet until its replacement credit follows.
                open_stakes[bet_id] = stakes[bet_id]
            elif entry.entry_type == EntryType.SETTLEMENT_CREDIT:
                open_stakes.pop(bet_id, None)
            history.append(entry.balance_after.amount + sum(open_stakes.values(), Decimal(0)))
        return tuple(history)

    @property
    def equity(self) -> Decimal:
        history = self.equity_history()
        return history[-1] if history else Decimal("0.00")

    @property
    def peak_equity(self) -> Decimal:
        return max(self.equity_history(), default=Decimal("0.00"))


class LedgerUnit(Protocol):
    """One transaction that holds an exclusive lock on one ledger."""

    def account(self) -> LedgerAccount | None: ...
    def create_account(self, account: LedgerAccount) -> None: ...
    def entries(self) -> Sequence[LedgerEntry]: ...
    def append(self, entry: LedgerEntry) -> None: ...
    def bets(self) -> Sequence[VirtualBet]: ...
    def add_bet(self, bet: VirtualBet) -> None: ...
    def settlements(self, bet_id: UUID) -> Sequence[SettlementRecord]: ...
    def add_settlement(self, record: SettlementRecord) -> None: ...


class LedgerStore(Protocol):
    def unit(self, ledger_id: str) -> AbstractContextManager[LedgerUnit]: ...


class _MemoryUnit:
    def __init__(self, state: dict[str, object], ledger_id: str) -> None:
        self._state = state
        self._ledger_id = ledger_id
        self._pending: dict[str, list[object]] = {"entries": [], "bets": [], "settlements": []}
        self._account: LedgerAccount | None = None

    def _saved(self, name: str) -> list[object]:
        saved = self._state.setdefault(f"{self._ledger_id}:{name}", [])
        assert isinstance(saved, list)
        return saved

    def account(self) -> LedgerAccount | None:
        saved = self._state.get(f"{self._ledger_id}:account")
        assert saved is None or isinstance(saved, LedgerAccount)
        return self._account or saved

    def create_account(self, account: LedgerAccount) -> None:
        self._account = account

    def entries(self) -> Sequence[LedgerEntry]:
        rows = self._saved("entries") + self._pending["entries"]
        return [row for row in rows if isinstance(row, LedgerEntry)]

    def append(self, entry: LedgerEntry) -> None:
        self._pending["entries"].append(entry)

    def bets(self) -> Sequence[VirtualBet]:
        rows = self._saved("bets") + self._pending["bets"]
        return [row for row in rows if isinstance(row, VirtualBet)]

    def add_bet(self, bet: VirtualBet) -> None:
        self._pending["bets"].append(bet)

    def settlements(self, bet_id: UUID) -> Sequence[SettlementRecord]:
        rows = self._saved("settlements") + self._pending["settlements"]
        return [row for row in rows if isinstance(row, SettlementRecord) and row.bet_id == bet_id]

    def add_settlement(self, record: SettlementRecord) -> None:
        self._pending["settlements"].append(record)

    def commit(self) -> None:
        if self._account is not None:
            self._state[f"{self._ledger_id}:account"] = self._account
        for name, rows in self._pending.items():
            self._saved(name).extend(rows)


class InMemoryLedgerStore:
    """Deterministic store for tests; one lock serializes all ledger transactions."""

    def __init__(self) -> None:
        self._state: dict[str, object] = {}
        self._lock = Lock()

    @contextmanager
    def unit(self, ledger_id: str) -> Iterator[LedgerUnit]:
        with self._lock:
            unit = _MemoryUnit(self._state, ledger_id)
            yield unit
            unit.commit()


def _active_settlement(entries: Sequence[LedgerEntry], bet_id: UUID) -> LedgerEntry | None:
    reversed_ids = {entry.reverses_entry_id for entry in entries if entry.reverses_entry_id}
    active = [
        entry
        for entry in entries
        if entry.bet_id == bet_id
        and entry.entry_type == EntryType.SETTLEMENT_CREDIT
        and entry.entry_id not in reversed_ids
    ]
    if len(active) > 1:
        raise AssertionError("A bet cannot have two active settlement entries")
    return active[0] if active else None


class VirtualLedgerService:
    def __init__(self, store: LedgerStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def _entry(
        self,
        unit: LedgerUnit,
        account: LedgerAccount,
        entry_type: EntryType,
        key: str,
        amount: Decimal,
        reason: str,
        **fields: object,
    ) -> LedgerEntry:
        entries = unit.entries()
        balance = entries[-1].balance_after.amount if entries else Decimal("0.00")
        entry = LedgerEntry.model_validate(
            {
                "entry_id": stable_id("ledger-entry", f"{account.ledger_id}:{key}"),
                "ledger_id": account.ledger_id,
                "sequence": len(entries) + 1,
                "entry_type": entry_type,
                "idempotency_key": key,
                "amount": Money(amount=amount, currency=account.currency),
                "balance_after": Money(amount=balance + amount, currency=account.currency),
                "reason": reason,
                "recorded_at": self.clock.now(),
            }
            | fields
        )
        unit.append(entry)
        return entry

    @staticmethod
    def _existing(unit: LedgerUnit, key: str) -> LedgerEntry | None:
        return next((entry for entry in unit.entries() if entry.idempotency_key == key), None)

    @staticmethod
    def _require_account(unit: LedgerUnit, ledger_id: str) -> LedgerAccount:
        account = unit.account()
        if account is None:
            raise LedgerConflict(f"Unknown ledger: {ledger_id}")
        if account.scope != LedgerScope.VIRTUAL:
            raise LedgerConflict("This service records only virtual ledgers")
        return account

    def open_ledger(self, ledger_id: str, opening_balance: Money) -> LedgerAccount:
        account = LedgerAccount(
            ledger_id=ledger_id,
            scope=LedgerScope.VIRTUAL,
            currency=opening_balance.currency,
            opening_balance=opening_balance,
            created_at=self.clock.now(),
        )
        with self.store.unit(ledger_id) as unit:
            existing = unit.account()
            if existing is not None:
                if existing.opening_balance != opening_balance or existing.scope != account.scope:
                    raise LedgerConflict("The ledger already exists with other terms")
                return existing
            unit.create_account(account)
            self._entry(
                unit,
                account,
                EntryType.OPENING_BALANCE,
                "opening",
                opening_balance.amount,
                "Virtual opening balance",
            )
            return account

    def record_virtual_bet(self, bet: VirtualBet) -> LedgerEntry:
        key = f"stake:{bet.bet_id}"
        with self.store.unit(bet.ledger_id) as unit:
            account = self._require_account(unit, bet.ledger_id)
            known = next((item for item in unit.bets() if item.bet_id == bet.bet_id), None)
            if known is not None:
                if known != bet:
                    raise LedgerConflict("The bet ID was reused with different terms")
                existing = self._existing(unit, key)
                assert existing is not None
                return existing
            if bet.stake.currency != account.currency:
                raise LedgerConflict("The bet currency differs from the ledger currency")
            entries = unit.entries()
            if bet.stake.amount > entries[-1].balance_after.amount:
                raise InsufficientFunds("The virtual stake exceeds the available balance")
            unit.add_bet(bet)
            return self._entry(
                unit,
                account,
                EntryType.STAKE_DEBIT,
                key,
                -bet.stake.amount,
                f"Virtual stake for decision {bet.decision_id}",
                bet_id=bet.bet_id,
            )

    def apply_settlement(
        self, ledger_id: str, result: SettlementResult, *, correction_reason: str | None = None
    ) -> tuple[LedgerEntry, ...]:
        """Record a settlement. A changed final result needs an explicit correction reason."""
        digest = result.financial_digest()
        with self.store.unit(ledger_id) as unit:
            account = self._require_account(unit, ledger_id)
            bet = next((item for item in unit.bets() if item.bet_id == result.bet_id), None)
            if bet is None:
                raise LedgerConflict("Settlement for an unknown bet")
            if result.stake_deducted != bet.stake:
                raise LedgerConflict("The settlement stake differs from the recorded stake")
            if result.rule_version != bet.settlement_rule_version:
                raise LedgerConflict("The settlement used another rule version than the bet")
            if not any(record.digest == digest for record in unit.settlements(bet.bet_id)):
                unit.add_settlement(
                    SettlementRecord(
                        record_id=stable_id("settlement-record", f"{bet.bet_id}:{digest}"),
                        bet_id=bet.bet_id,
                        digest=digest,
                        result=result,
                        recorded_at=self.clock.now(),
                    )
                )
            active = _active_settlement(unit.entries(), bet.bet_id)
            if active is not None and active.settlement_digest == digest:
                return (active,)  # Retry of the effect already recorded.
            if result.status == SettlementStatus.PENDING:
                if active is not None:
                    raise LedgerConflict("A final settlement cannot silently return to pending")
                return ()
            assert result.cash_return is not None
            created: list[LedgerEntry] = []
            revision = 0
            if active is not None:
                if not correction_reason:
                    raise LedgerConflict("A different final result requires a correction")
                revision = sum(
                    1
                    for entry in unit.entries()
                    if entry.bet_id == bet.bet_id and entry.entry_type == EntryType.REVERSAL
                )
                created.append(
                    self._entry(
                        unit,
                        account,
                        EntryType.REVERSAL,
                        f"reverse:{active.entry_id}",
                        -active.amount.amount,
                        f"Correction: {correction_reason}",
                        bet_id=bet.bet_id,
                        reverses_entry_id=active.entry_id,
                        settlement_digest=active.settlement_digest,
                        settlement_status=active.settlement_status,
                    )
                )
            created.append(
                self._entry(
                    unit,
                    account,
                    EntryType.SETTLEMENT_CREDIT,
                    f"settle:{bet.bet_id}:{digest}:{revision + (1 if active else 0)}",
                    result.cash_return.amount,
                    correction_reason or f"Settlement {result.status.value}",
                    bet_id=bet.bet_id,
                    settlement_digest=digest,
                    settlement_status=result.status,
                )
            )
            return tuple(created)

    def view(self, ledger_id: str) -> LedgerView:
        """A consistent read of one virtual ledger, for F12 exposure calculations."""
        with self.store.unit(ledger_id) as unit:
            account = self._require_account(unit, ledger_id)
            entries = tuple(unit.entries())
            bets = tuple(unit.bets())
            open_ids = frozenset(
                bet.bet_id for bet in bets if _active_settlement(entries, bet.bet_id) is None
            )
            return LedgerView(account=account, entries=entries, bets=bets, open_bet_ids=open_ids)

    def reconcile(self, ledger_id: str) -> LedgerReconciliation:
        with self.store.unit(ledger_id) as unit:
            account = self._require_account(unit, ledger_id)
            entries = unit.entries()
            currency = account.currency
            total = sum((entry.amount.amount for entry in entries), Decimal("0.00"))
            running = Decimal("0.00")
            chain_ok = True
            for index, entry in enumerate(entries, start=1):
                running += entry.amount.amount
                chain_ok &= entry.sequence == index and entry.balance_after.amount == running
            open_exposure = Decimal("0.00")
            open_bets = settled = pending = 0
            for bet in unit.bets():
                if _active_settlement(entries, bet.bet_id) is None:
                    open_bets += 1
                    open_exposure += bet.stake.amount
                    pending += bool(unit.settlements(bet.bet_id))
                else:
                    settled += 1
            closing = entries[-1].balance_after.amount if entries else Decimal("0.00")
            return LedgerReconciliation(
                ledger_id=ledger_id,
                opening_balance=account.opening_balance,
                total_deltas=Money(amount=total, currency=currency),
                closing_balance=Money(amount=closing, currency=currency),
                open_exposure=Money(amount=open_exposure, currency=currency),
                open_bets=open_bets,
                settled_bets=settled,
                pending_bets=pending,
                entries=len(entries),
                balanced=chain_ok
                and closing == total
                and bool(entries)
                and entries[0].entry_type == EntryType.OPENING_BALANCE
                and entries[0].amount == account.opening_balance,
            )
