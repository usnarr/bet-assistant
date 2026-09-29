"""PostgreSQL persistence for the F06 append-only virtual ledger.

Each unit of work runs in one transaction under a transaction-scoped advisory lock on
the ledger ID, so concurrent workers serialize per ledger. Database triggers reject
updates and deletes; uniqueness constraints back the idempotency keys.
"""

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from .ledger import LedgerAccount, LedgerEntry, LedgerUnit, SettlementRecord, VirtualBet


def _json(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class _PostgresUnit:
    def __init__(self, connection: Connection, ledger_id: str) -> None:
        self._db = connection
        self._ledger_id = ledger_id

    def account(self) -> LedgerAccount | None:
        row = self._db.execute(
            text(
                "SELECT ledger_id, scope, currency, opening_balance, created_at "
                "FROM tennis.settlement_ledger WHERE ledger_id = :ledger_id"
            ),
            {"ledger_id": self._ledger_id},
        ).one_or_none()
        if row is None:
            return None
        data = row._mapping
        return LedgerAccount.model_validate(
            {
                "ledger_id": data["ledger_id"],
                "scope": data["scope"],
                "currency": data["currency"],
                "opening_balance": {
                    "amount": data["opening_balance"],
                    "currency": data["currency"],
                },
                "created_at": data["created_at"],
            }
        )

    def create_account(self, account: LedgerAccount) -> None:
        self._db.execute(
            text(
                "INSERT INTO tennis.settlement_ledger "
                "(ledger_id, scope, currency, opening_balance, created_at) "
                "VALUES (:ledger_id, :scope, :currency, :opening, :created_at)"
            ),
            {
                "ledger_id": account.ledger_id,
                "scope": account.scope.value,
                "currency": account.currency.value,
                "opening": account.opening_balance.amount,
                "created_at": account.created_at,
            },
        )

    def entries(self) -> Sequence[LedgerEntry]:
        rows = self._db.execute(
            text(
                "SELECT * FROM tennis.settlement_ledger_entry "
                "WHERE ledger_id = :ledger_id ORDER BY sequence"
            ),
            {"ledger_id": self._ledger_id},
        )
        entries = []
        for row in rows:
            data = dict(row._mapping)
            currency = data.pop("currency")
            data["amount"] = {"amount": data["amount"], "currency": currency}
            data["balance_after"] = {"amount": data["balance_after"], "currency": currency}
            entries.append(LedgerEntry.model_validate(data))
        return entries

    def append(self, entry: LedgerEntry) -> None:
        self._db.execute(
            text(
                "INSERT INTO tennis.settlement_ledger_entry "
                "(entry_id, ledger_id, sequence, entry_type, idempotency_key, amount, "
                "balance_after, currency, bet_id, settlement_digest, settlement_status, "
                "reverses_entry_id, reason, recorded_at) VALUES (:entry_id, :ledger_id, "
                ":sequence, :entry_type, :key, :amount, :balance_after, :currency, :bet_id, "
                ":digest, :status, :reverses, :reason, :recorded_at)"
            ),
            {
                "entry_id": entry.entry_id,
                "ledger_id": entry.ledger_id,
                "sequence": entry.sequence,
                "entry_type": entry.entry_type.value,
                "key": entry.idempotency_key,
                "amount": entry.amount.amount,
                "balance_after": entry.balance_after.amount,
                "currency": entry.amount.currency.value,
                "bet_id": entry.bet_id,
                "digest": entry.settlement_digest,
                "status": entry.settlement_status.value if entry.settlement_status else None,
                "reverses": entry.reverses_entry_id,
                "reason": entry.reason,
                "recorded_at": entry.recorded_at,
            },
        )

    def bets(self) -> Sequence[VirtualBet]:
        rows = self._db.execute(
            text(
                "SELECT payload FROM tennis.settlement_virtual_bet "
                "WHERE ledger_id = :ledger_id ORDER BY recorded_at, bet_id"
            ),
            {"ledger_id": self._ledger_id},
        )
        return [VirtualBet.model_validate(row[0]) for row in rows]

    def add_bet(self, bet: VirtualBet) -> None:
        self._db.execute(
            text(
                "INSERT INTO tennis.settlement_virtual_bet "
                "(bet_id, ledger_id, decision_id, stake, payload, recorded_at) "
                "VALUES (:bet_id, :ledger_id, :decision_id, :stake, "
                "CAST(:payload AS JSONB), :recorded_at)"
            ),
            {
                "bet_id": bet.bet_id,
                "ledger_id": bet.ledger_id,
                "decision_id": bet.decision_id,
                "stake": bet.stake.amount,
                "payload": _json(bet),
                "recorded_at": bet.struck_at,
            },
        )

    def settlements(self, bet_id: UUID) -> Sequence[SettlementRecord]:
        rows = self._db.execute(
            text(
                "SELECT record_id, bet_id, digest, payload, recorded_at "
                "FROM tennis.settlement_record WHERE bet_id = :bet_id ORDER BY recorded_at"
            ),
            {"bet_id": bet_id},
        )
        return [
            SettlementRecord.model_validate(
                {
                    "record_id": row.record_id,
                    "bet_id": row.bet_id,
                    "digest": row.digest,
                    "result": row.payload,
                    "recorded_at": row.recorded_at,
                }
            )
            for row in rows
        ]

    def add_settlement(self, record: SettlementRecord) -> None:
        self._db.execute(
            text(
                "INSERT INTO tennis.settlement_record "
                "(record_id, bet_id, digest, status, rule_version, payload, recorded_at) "
                "VALUES (:record_id, :bet_id, :digest, :status, :rule_version, "
                "CAST(:payload AS JSONB), :recorded_at)"
            ),
            {
                "record_id": record.record_id,
                "bet_id": record.bet_id,
                "digest": record.digest,
                "status": record.result.status.value,
                "rule_version": record.result.rule_version,
                "payload": _json(record.result),
                "recorded_at": record.recorded_at,
            },
        )


class PostgresLedgerStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def unit(self, ledger_id: str) -> Iterator[LedgerUnit]:
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('settlement-ledger:' || :ledger_id))"),
                {"ledger_id": ledger_id},
            )
            yield _PostgresUnit(connection, ledger_id)
