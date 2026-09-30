"""F14.5 deterministic explanations from canonical facts and gate outputs.

Every number in a sentence is the exact stored value, written with `str(Decimal)`. The
templates state only facts, model estimates, payout arithmetic, missing evidence and gate
results. They make no claim about motivation, injuries, certainty or profit.
"""

from collections.abc import Callable
from decimal import Decimal

from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.pricing.decision import Gate

from .contracts import EvidenceFact, FactKind, ReasonView, Statement, StoredDecision

GATE_TEXT: dict[str, str] = {
    Gate.DECISION_POLICY: "The decision policy is not approved or not in effect.",
    Gate.IDENTITY: "The event or player identity is not resolved.",
    Gate.NOT_STARTED: "The match start state does not permit a pre-match decision.",
    Gate.QUOTE_FRESH: "The quote is not fresh or not actionable.",
    Gate.MARKET: "The market or selection is not supported or not mapped.",
    Gate.RULES: "A settlement rule, payout rule or publication permission is not available.",
    Gate.QUALITY: "The feature data quality is not sufficient.",
    Gate.MODEL_DOMAIN: "The model does not support this match.",
    Gate.MODEL_CALIBRATED: "The model calibration is not demonstrated.",
    Gate.MODEL_DISAGREEMENT: "The models disagree more than the policy permits.",
    Gate.CENTRAL_EV: "The central expected value is not positive.",
    Gate.CONSERVATIVE_EV: "The conservative expected value is not positive.",
    Gate.CONSERVATIVE_ROI: "The conservative return is below the policy minimum.",
    Gate.OUTLIER: "A large edge is not confirmed by the market consensus.",
    Gate.RISK_BUDGET: "No risk budget is available.",
    Gate.RESPONSIBLE_USE: "Responsible-use limits do not permit a recommendation.",
    Gate.STAKE: "No valid stake is available.",
}

NO_PLACEMENT = "Confirm the quote manually before any action. This system places no bets."


def reasons(stored: StoredDecision) -> tuple[ReasonView, ...]:
    return tuple(
        ReasonView(
            gate=item.gate.value,
            code=item.code,
            details=item.detail,
            text=GATE_TEXT.get(item.gate, "A decision gate failed."),
        )
        for item in stored.record.gates
        if not item.passed
    )


def signed(value: Decimal) -> str:
    return f"+{value}" if value > 0 else str(value)


def _with_unit(value: Decimal, unit: str) -> str:
    return f"{value} {unit}" if unit else str(value)


def fact_statement(fact: EvidenceFact, selection: str, opponent: str, withheld: bool) -> Statement:
    base = {"fact_key": fact.key, "source_id": fact.source_id}
    if withheld:
        text = f"{fact.label}: the value is withheld. The source does not permit redistribution."
        return Statement(kind="WITHHELD", text=text, **base)
    if fact.kind == FactKind.MISSING:
        return Statement(kind="MISSING", text=f"{fact.label}: no verified value.", **base)
    prefix = "Model estimate. " if fact.kind == FactKind.INFERRED else ""
    parts = []
    for name, value in ((selection, fact.selection_value), (opponent, fact.opponent_value)):
        parts.append(
            f"{name}: no verified value"
            if value is None
            else f"{name}: {_with_unit(value, fact.unit)}"
        )
    text = f"{prefix}{fact.label}. {'; '.join(parts)}."
    difference = fact.difference
    if difference is not None:
        text += f" Difference: {signed(difference)}{' ' + fact.unit if fact.unit else ''}."
    return Statement(kind=fact.kind.value, text=text, **base)


def explain(stored: StoredDecision, withhold: Callable[[str], bool]) -> tuple[Statement, ...]:
    """`withhold(source_id)` is True when the viewer may not see that source's values."""
    record, context = stored.record, stored.context
    selected = context.match.player(record.selection_player_id)
    opposed = context.match.opponent(record.selection_player_id)
    selection = selected.display_name if selected else "Selected player"
    opponent = opposed.display_name if opposed else "Opponent"
    statements: list[Statement] = []

    if record.decimal_odds is not None and context.quote_source_id is not None:
        if withhold(context.quote_source_id):
            statements.append(
                Statement(
                    kind="WITHHELD",
                    text="The quoted odds are withheld. The source does not permit redistribution.",
                    source_id=context.quote_source_id,
                )
            )
        else:
            statements.append(
                Statement(
                    kind="OBSERVED",
                    text=f"{record.bookmaker} quoted {record.decimal_odds} for {selection}.",
                    source_id=context.quote_source_id,
                )
            )
    else:
        statements.append(Statement(kind="MISSING", text="No quote is available."))

    for fact in context.facts:
        statements.append(fact_statement(fact, selection, opponent, withhold(fact.source_id)))

    if record.central_probability is None or record.conservative_probability is None:
        statements.append(Statement(kind="MISSING", text="No model probability is available."))
    else:
        statements.append(
            Statement(
                kind="INFERRED",
                text=f"The model probability for {selection} is {record.central_probability} "
                f"({record.probability_semantics}). The conservative probability is "
                f"{record.conservative_probability}.",
            )
        )

    value = record.value
    if value is None:
        statements.append(Statement(kind="MISSING", text="No payout or value result exists."))
    else:
        statements.append(
            Statement(
                kind="DERIVED",
                text=f"At a stake of {value.stake.amount} {value.stake.currency}, a win returns "
                f"{value.cash_return_if_win.amount} {value.cash_return_if_win.currency}. The "
                f"net break-even probability is {value.break_even_probability}.",
            )
        )
        statements.append(
            Statement(
                kind="DERIVED",
                text=f"Expected value: {value.expected_value}. Conservative expected value: "
                f"{value.conservative_expected_value}. Conservative return: "
                f"{value.conservative_roi}.",
            )
        )

    for item in reasons(stored):
        detail = f" Details: {', '.join(item.details)}." if item.details else ""
        statements.append(Statement(kind="DECISION", text=f"{item.text}{detail}"))

    if record.status == RecommendationStatus.BET:
        closing = (
            f"Decision: BET. Virtual stake {record.stake.amount} {record.stake.currency}. "
            f"{NO_PLACEMENT}"
        )
    else:
        closing = f"Decision: {record.status.value}. No stake is recommended."
    statements.append(Statement(kind="DECISION", text=closing))
    return tuple(statements)
