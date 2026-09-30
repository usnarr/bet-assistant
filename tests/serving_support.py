"""Synthetic F14 fixtures: stored decisions, fake read checks and API clients.

All names, values and tokens are synthetic. They are not real players or credentials.
"""

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from settlement_support import MATCH_ID, PLAYER_A, PLAYER_B
from test_foundation_api import Probe
from test_pricing_decision import AT, fresh, inputs, model, ref
from test_pricing_publication import observation

from tennis_engine.common.clock import FrozenClock
from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.governance.contracts import Decision, Role
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.pricing.decision import decide, volatile_problems
from tennis_engine.serving.api import Serving, create_app
from tennis_engine.serving.auth import ApiCredential, TokenAuthenticator, token_digest
from tennis_engine.serving.checks import StaticRedistribution
from tennis_engine.serving.contracts import (
    DecisionContext,
    EvidenceFact,
    FactKind,
    MatchSummary,
    ModelComponent,
    PlayerRef,
    ResponsibleUseStatus,
    StoredDecision,
)
from tennis_engine.serving.service import RecommendationService
from tennis_engine.serving.store import InMemoryDecisionStore

D = Decimal
READ_AT = AT + timedelta(seconds=5)
TOKENS = {
    Role.DASHBOARD: "synthetic-dashboard-token",
    Role.AGENT: "synthetic-agent-token",
    Role.OPERATOR: "synthetic-operator-token",
    Role.POLICY_REVIEWER: "synthetic-reviewer-token",
}
BOOK_SOURCE = "synthetic-book"
SPORTS_SOURCE = "synthetic-sports"


def headers(role=Role.OPERATOR):
    return {"Authorization": f"Bearer {TOKENS[role]}"}


def authenticator():
    return TokenAuthenticator(
        ApiCredential(identity=f"fixture-{role.value}", role=role, token_sha256=token_digest(t))
        for role, t in TOKENS.items()
    )


MATCH = MatchSummary(
    match_id=MATCH_ID,
    tournament_name="Synthetic Open",
    tour="ATP",
    surface="HARD",
    scheduled_start=AT + timedelta(hours=3),
    players=(
        PlayerRef(player_id=PLAYER_A, display_name="Player Alpha"),
        PlayerRef(player_id=PLAYER_B, display_name="Player Beta"),
    ),
)

FACTS = (
    EvidenceFact(
        key="surface_elo",
        label="Surface Elo rating",
        kind=FactKind.INFERRED,
        unit="Elo points",
        selection_value=D("1850.5"),
        opponent_value=D("1778.0"),
        as_of=AT - timedelta(hours=1),
        source_id=SPORTS_SOURCE,
    ),
    EvidenceFact(
        key="matches_90d",
        label="Matches played in the last 90 days",
        kind=FactKind.OBSERVED,
        unit="matches",
        selection_value=D("14"),
        opponent_value=D("9"),
        as_of=AT - timedelta(hours=1),
        source_id=SPORTS_SOURCE,
    ),
    EvidenceFact(
        key="minutes_48h",
        label="Minutes played in the last 48 hours",
        kind=FactKind.MISSING,
        unit="minutes",
        as_of=AT - timedelta(hours=1),
        source_id=SPORTS_SOURCE,
    ),
)


def context(record, **overrides):
    has_quote = record.quote_id is not None
    data = {
        "decision_id": record.decision_id,
        "match": MATCH,
        "account_scope": "shadow",
        "quote_source_id": BOOK_SOURCE if has_quote else None,
        "quote_observed_at": AT - timedelta(seconds=10) if has_quote else None,
        "quote_key": ("synthetic-book", "e-1", "m-1", "s-1") if has_quote else None,
        "source_ids": (BOOK_SOURCE, SPORTS_SOURCE),
        "facts": FACTS,
        "components": (
            ModelComponent(model=ref("synthetic-model"), role="PRIMARY", probability=D("0.60")),
        ),
    }
    return DecisionContext.model_validate(data | overrides)


def stored(record, **overrides):
    return StoredDecision(record=record, context=context(record, **overrides))


def bet():
    return decide(inputs())


def watch():
    return decide(inputs(decision_key="decision-watch", model=model("0.60", "0.47")))


def no_bet():
    return decide(inputs(decision_key="decision-no-bet", model=model("0.45", "0.40")))


def no_quote():
    return decide(inputs(decision_key="decision-no-quote", quote=None, model=None))


def copy(record, key, **update):
    """A distinct synthetic record for filter and pagination fixtures."""
    return record.model_copy(
        update={"decision_id": stable_id("decision", key), "decision_key": key} | update
    )


class FakeChecks:
    """Read checks with settable state. BETs use the shared F12 volatile recheck."""

    def __init__(self):
        self.disabled: dict[str, str] = {}
        self.responsible = PolicyLookup(
            Decision(allowed=True, version="synthetic-responsible-v1", revision=1), object()
        )
        self.actionability = fresh(observation=observation())
        self.fail = False
        self.calls = 0

    def source_state(self, source_id, now):
        return self.disabled.get(source_id)

    def responsible_use(self, account_scope, now):
        return ResponsibleUseStatus(
            account_scope=account_scope,
            allowed=self.responsible.decision.allowed,
            reason=self.responsible.decision.reason.value,
            policy_version=self.responsible.decision.version,
        )

    def check(self, item, now):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic dependency failure")
        reasons = [f"SOURCE:{s}:{r}" for s, r in sorted(self.disabled.items())]
        if item.record.status == RecommendationStatus.BET:
            reasons += [
                detail
                for _, detail in volatile_problems(
                    item.record,
                    now=now,
                    actionability=self.actionability,
                    responsible_use=self.responsible,
                )
            ]
        return tuple(reasons)


def build(items=(), *, checks=None, redistribution=(BOOK_SOURCE, SPORTS_SOURCE), at=READ_AT):
    store = InMemoryDecisionStore()
    for item in items:
        store.add(item)
    clock = FrozenClock(at)
    service = RecommendationService(
        store=store,
        checks=checks or FakeChecks(),
        clock=clock,
        redistribution=StaticRedistribution(redistribution),
        bookmakers=frozenset({"synthetic-book", "other-book"}),
    )
    app = create_app(Settings(environment="test"), Probe(True), Serving(service, authenticator()))
    return TestClient(app), service, store, clock
