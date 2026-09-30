"""Synthetic F12 policy and exposure builders for tests. Values are not accepted policy."""

from settlement_support import reviewed

from tennis_engine.governance.contracts import ResponsibleUsePolicy
from tennis_engine.pricing.risk import DecisionPolicy, ExposureState


def decision_policy(**overrides):
    return DecisionPolicy.model_validate(
        reviewed("synthetic-decision-v1")
        | {
            "state": "APPROVED",
            "kelly_fraction": "0.20",
            "minimum_conservative_roi": "0.02",
            "max_model_disagreement": "0.05",
            "max_unconfirmed_edge": "0.10",
            "consensus_tolerance": "0.03",
            "max_bookmaker_exposure": "80.00",
            "max_open_bets": 20,
            "reservation_ttl_seconds": 120,
            "no_bet_ttl_seconds": 60,
        }
        | overrides
    )


def responsible(**overrides):
    return ResponsibleUsePolicy.model_validate(
        reviewed("synthetic-responsible-v1")
        | {
            "account_scope": "shadow",
            "ledger_scope": "virtual",
            "state": "APPROVED",
            "daily": {"stake": "60.00", "count": 5},
            "weekly": {"stake": "200.00", "count": 20},
            "monthly": {"stake": "500.00", "count": 60},
            "max_event_exposure": "50.00",
            "max_open_exposure": "150.00",
            "max_bankroll_fraction": "0.05",
            "drawdown_stop": "0.20",
            "disable_recommendations": False,
        }
        | overrides
    )


def exposure(bankroll="1000.00", **overrides):
    usage = {"stake": "0.00", "count": 0}
    return ExposureState.model_validate(
        {
            "bankroll": bankroll,
            "equity": bankroll,
            "peak_bankroll": bankroll,
            "open_exposure": "0.00",
            "event_exposure": "0.00",
            "bookmaker_exposure": "0.00",
            "open_bets": 0,
            "daily": usage,
            "weekly": usage,
            "monthly": usage,
        }
        | overrides
    )
