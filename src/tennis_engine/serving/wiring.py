"""F14 production wiring: build the read-only serving stack from typed settings.

`create_production_app` is the process entry point (`uvicorn --factory`). It fails closed:

- Serving off: every F14 route returns HTTP 503.
- Production with a missing credential file, an empty credential list, a placeholder
  token or an unreadable governance journal: the process refuses to start.
- Development with a missing credential file: every F14 request gets HTTP 401.
- A journal or quote store that fails at read time: the record is served as NO_BET.
- A decision store that fails at read time: HTTP 503.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import Engine

from tennis_engine.common.clock import Clock, SystemClock
from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.governance.service import GovernanceService
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.database import build_engine
from tennis_engine.infrastructure.health import Check, InfrastructureProbe, ReadinessProbe
from tennis_engine.infrastructure.settings import PLACEHOLDER_SECRETS, Settings
from tennis_engine.ingestion.bookmakers.history import HistoryStore, QuoteHistory
from tennis_engine.ingestion.bookmakers.postgres import PostgresHistoryStore
from tennis_engine.ingestion.bookmakers.quotes import ActionabilityPolicy

from .api import Serving, create_app
from .auth import TokenAuthenticator, load_credentials, token_digest
from .checks import (
    GovernanceFactory,
    GovernanceReadChecks,
    GovernanceRedistribution,
    history_actionability,
    per_thread,
)
from .postgres import PostgresDecisionStore
from .service import RecommendationService
from .store import DecisionStore

logger = logging.getLogger("tennis_engine.serving")

# The API reads governance with a viewer role. SQLite read-only mode rejects all writes.
SERVING_PRINCIPAL = Principal(identity="serving-api", role=Role.DASHBOARD)
PLACEHOLDER_DIGESTS = frozenset(token_digest(value) for value in PLACEHOLDER_SECRETS)


class ServingConfigurationError(RuntimeError):
    """The serving configuration is not safe to start. The message holds no secret."""


def governance_factory(journal: Path) -> GovernanceFactory:
    """Open a read-only governance service. Each call opens a new connection."""

    def open_governance() -> GovernanceService:
        return GovernanceService(GovernanceStore(journal, SERVING_PRINCIPAL, read_only=True))

    return open_governance


def journal_check(journal: Path, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Check:
    """Readiness of the governance journal. The detail shows no path or content."""
    try:
        store = GovernanceStore(journal, SERVING_PRINCIPAL, read_only=True)
    except Exception as error:  # noqa: BLE001 - any failure means not ready
        return Check(False, f"{type(error).__name__}: governance journal unavailable")
    try:
        stopped = store.global_disabled(now())
    except Exception as error:  # noqa: BLE001 - any failure means not ready
        return Check(False, f"{type(error).__name__}: governance journal unreadable")
    finally:
        store.close()
    return Check(True, f"read-only; global_stop={'on' if stopped else 'off'}")


def load_authenticator(settings: Settings) -> TokenAuthenticator:
    path = settings.api_credentials_file
    if path is None:
        raise ServingConfigurationError("TENNIS_API_CREDENTIALS_FILE is not set")
    if not path.is_file():
        if settings.environment == "production":
            raise ServingConfigurationError("The API credential file does not exist")
        logger.warning("API credential file missing; every F14 request is refused")
        return TokenAuthenticator(())
    try:
        credentials = load_credentials(path)
    except (OSError, ValueError, ValidationError) as error:
        # A JSON or validation error can echo file content, so do not include it.
        raise ServingConfigurationError(
            f"The API credential file is not valid ({type(error).__name__})"
        ) from None
    if settings.environment == "production" and not credentials:
        raise ServingConfigurationError("The API credential file has no credentials")
    if any(item.token_sha256 in PLACEHOLDER_DIGESTS for item in credentials):
        raise ServingConfigurationError("The API credential file contains a placeholder token")
    try:
        return TokenAuthenticator(credentials)
    except ValueError as error:
        raise ServingConfigurationError(str(error)) from None


def build_recommendation_service(
    settings: Settings,
    *,
    engine: Engine | None = None,
    decision_store: DecisionStore | None = None,
    history_store: HistoryStore | None = None,
    clock: Clock | None = None,
    actionability_policy: ActionabilityPolicy | None = None,
) -> RecommendationService:
    """The read-only F14 service without an authenticator. The scheduler uses it for
    source-health signals."""
    if settings.governance_journal is None:
        raise ServingConfigurationError("TENNIS_GOVERNANCE_JOURNAL is not set")
    if decision_store is None or history_store is None:
        engine = engine or build_engine(
            settings.serving_database(), connect_timeout=settings.serving_connect_timeout_seconds
        )
        decision_store = decision_store or PostgresDecisionStore(engine)
        history_store = history_store or PostgresHistoryStore(engine)
    governance = per_thread(governance_factory(settings.governance_journal))
    history = QuoteHistory(history_store)
    return RecommendationService(
        store=decision_store,
        checks=GovernanceReadChecks(
            governance,
            history_actionability(history, actionability_policy or ActionabilityPolicy()),
        ),
        clock=clock or SystemClock(),
        redistribution=GovernanceRedistribution(governance),
        account_scope=settings.serving_account_scope,
        stale_after_seconds=settings.serving_stale_after_seconds,
    )


def build_serving(
    settings: Settings,
    *,
    engine: Engine | None = None,
    decision_store: DecisionStore | None = None,
    history_store: HistoryStore | None = None,
    clock: Clock | None = None,
    actionability_policy: ActionabilityPolicy | None = None,
) -> Serving:
    """Build the F14 service from the approved stores. Overrides exist for tests."""
    service = build_recommendation_service(
        settings,
        engine=engine,
        decision_store=decision_store,
        history_store=history_store,
        clock=clock,
        actionability_policy=actionability_policy,
    )
    return Serving(service, load_authenticator(settings))


class ServingProbe:
    """Infrastructure readiness plus the governance journal that serving needs."""

    def __init__(self, base: ReadinessProbe, journal: Path) -> None:
        self.base = base
        self.journal = journal

    def check(self) -> dict[str, Check]:
        checks = dict(self.base.check())
        checks["governance_journal"] = journal_check(self.journal)
        return checks


def create_production_app(settings: Settings | None = None) -> FastAPI:
    """Process entry point. Invalid serving configuration stops the start."""
    settings = settings or Settings()
    if not settings.serving_enabled:
        logger.warning("F14 serving is off; F14 routes return HTTP 503")
        return create_app(settings)
    assert settings.governance_journal is not None  # enforced by Settings
    if settings.environment == "production":
        state = journal_check(settings.governance_journal)
        if not state.ready:
            raise ServingConfigurationError(f"Governance journal check failed: {state.detail}")
    serving = build_serving(settings)
    probe = ServingProbe(InfrastructureProbe(settings), settings.governance_journal)
    logger.info(
        "F14 serving configured",
        extra={"context": settings.public_summary()},
    )
    return create_app(settings, probe, serving)
