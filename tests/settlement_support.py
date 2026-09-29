"""Synthetic, reviewer-style F06 rules for tests. Not real bookmaker rules or tax law.

The numbers are deliberately unlike any known Polish setting so that nobody mistakes a
fixture for policy: 10% stake tax, 20% winnings tax above PLN 1000.00.
"""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from tennis_engine.common.contracts import Money
from tennis_engine.governance.contracts import (
    Decision,
    PayoutPolicy,
)
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.settlement.rules import RULE_LIST, RuleRegistry

REVIEWED_AT = datetime(2026, 9, 1, tzinfo=UTC)
BET_TIME = datetime(2026, 9, 20, 12, tzinfo=UTC)
DOCUMENT = b"Synthetic rule document for F06 tests. Not a real bookmaker rule."
DOCUMENT_SHA256 = hashlib.sha256(DOCUMENT).hexdigest()
PLAYER_A = UUID("00000000-0000-4000-8000-00000000000a")
PLAYER_B = UUID("00000000-0000-4000-8000-00000000000b")
MATCH_ID = UUID("00000000-0000-4000-8000-0000000000aa")


def ref(section: str) -> dict[str, str]:
    return {"document_id": "synthetic-rules-doc", "section": section}


def reviewed(version: str, **overrides: Any) -> dict[str, Any]:
    return {
        "version": version,
        "owner": "fixture-owner",
        "reviewer": "fixture-reviewer",
        "reviewed_at": "2026-09-01T00:00:00Z",
        "review_due_at": "2027-09-01T00:00:00Z",
        "effective_from": "2026-09-01T00:00:00Z",
        "effective_until": "2027-09-01T00:00:00Z",
        "evidence": [{"document_id": "synthetic-rules-doc", "sha256": DOCUMENT_SHA256}],
        "state": "REVIEWED",
    } | overrides


def rounding(mode: str = "ROUND_DOWN") -> dict[str, str]:
    return {"quantum": "0.01", "mode": mode}


TAXES = {
    "stake_tax": {"rate": "0.10", "rounding": rounding("ROUND_HALF_UP"), "rule": ref("tax-1")},
    "winnings_tax": {
        "threshold": "1000.00",
        "threshold_basis": "gross_return",
        "comparison": "gt",
        "rate": "0.20",
        "base": "gross_return",
        "rounding": rounding("ROUND_HALF_UP"),
        "rule": ref("tax-2"),
    },
    "return_rounding": rounding("ROUND_DOWN"),
}


def payout_rule(version: str = "synthetic-book-payout-v1", **overrides: Any) -> dict[str, Any]:
    return (
        {"kind": "bookmaker_payout"}
        | reviewed(version)
        | {
            "bookmaker": "synthetic-book",
            "tax_treatment": "jurisdiction",
            "jurisdiction_rule_version": "synthetic-pl-payout-v1",
            "taxes": None,
            "minimum_stake": "2.00",
            "maximum_stake": "5000.00",
            "stake_increment": "0.01",
            "maximum_cash_return": "100000.00",
        }
        | overrides
    )


def settlement_rule(
    version: str = "synthetic-book-settlement-v1", **overrides: Any
) -> dict[str, Any]:
    return (
        {"kind": "bookmaker_settlement"}
        | reviewed(version)
        | {
            "bookmaker": "synthetic-book",
            "market": "TENNIS_MATCH_WINNER",
            "void_cash_return": "stake_deducted",
            "completed": {"action": "RESULT", "rule": ref("s-1")},
            "retirement": {
                "action": "ADVANCING_PLAYER",
                "minimum_completed_sets": 1,
                "rule": ref("s-2"),
            },
            "walkover": {"action": "VOID", "rule": ref("s-3")},
            "disqualification": {
                "action": "ADVANCING_PLAYER",
                "minimum_completed_sets": None,
                "rule": ref("s-4"),
            },
            "abandonment": {"action": "VOID", "minimum_completed_sets": None, "rule": ref("s-5")},
            "postponement": {"void_after_hours": 48, "rule": ref("s-6")},
            "venue_change": {"action": "RESULT", "rule": ref("s-7")},
            "surface_change": {"action": "VOID", "rule": ref("s-8")},
            "format_change": {"action": "VOID", "rule": ref("s-9")},
            "wrong_listing": {"action": "REVIEW", "rule": ref("s-10")},
            "palpable_error": {"action": "REVIEW", "rule": ref("s-11")},
        }
        | overrides
    )


def promotion_rule(version: str, kind: str, **overrides: Any) -> dict[str, Any]:
    return (
        {"kind": "promotion"}
        | reviewed(version)
        | {
            "promotion_id": version,
            "bookmaker": "synthetic-book",
            "promotion_kind": kind,
            "markets": ["TENNIS_MATCH_WINNER"],
            "eligible_account_scopes": ["shadow"],
            "minimum_odds": "1.50",
            "maximum_stake": "500.00",
            "rule": ref("p-1"),
        }
        | overrides
    )


def rule_documents() -> list[dict[str, Any]]:
    return [
        {"kind": "jurisdiction_payout"}
        | reviewed("synthetic-pl-payout-v1")
        | {"jurisdiction": "PL", "taxes": TAXES},
        payout_rule(),
        payout_rule(
            "synthetic-own-tax-payout-v1",
            tax_treatment="bookmaker",
            jurisdiction_rule_version=None,
            taxes=TAXES | {"stake_tax": None, "winnings_tax": None},
        ),
        payout_rule("synthetic-draft-payout-v1", state="DRAFT"),
        settlement_rule(),
        settlement_rule(
            "synthetic-book-settlement-sparse-v1",
            retirement={"action": "VOID", "minimum_completed_sets": None, "rule": ref("x-2")},
            walkover=None,
            postponement=None,
            surface_change=None,
        ),
        promotion_rule("synthetic-tax-covered-v1", "STAKE_TAX_COVERED"),
        promotion_rule("synthetic-freebet-v1", "FREEBET"),
    ]


def registry(extra: list[dict[str, Any]] | None = None, **kwargs: Any) -> RuleRegistry:
    return RuleRegistry(RULE_LIST.validate_python(rule_documents() + (extra or [])), **kwargs)


def policy(**overrides: Any) -> PayoutPolicy:
    return PayoutPolicy.model_validate(
        reviewed("synthetic-policy-v1")
        | {
            "bookmaker": "synthetic-book",
            "state": "APPROVED",
            "payout_rule_version": "synthetic-book-payout-v1",
            "settlement_rule_version": "synthetic-book-settlement-v1",
            "promotion_handling": "reviewed_rules",
            "promotion_rule_version": "synthetic-tax-covered-v1",
        }
        | {key: value for key, value in overrides.items()}
    )


def allowed(payout_policy: PayoutPolicy | None = None) -> PolicyLookup[PayoutPolicy]:
    payout_policy = payout_policy or policy()
    return PolicyLookup(
        Decision(allowed=True, version=payout_policy.version, revision=1), payout_policy
    )


def pln(amount: str) -> Money:
    return Money(amount=Decimal(amount))
