"""Internal API: health endpoints plus the F14 read-only recommendation routes.

Every F14 route is GET only and needs an authenticated principal. The server checks each
permission. Responses are not cacheable, so a cache cannot keep a record actionable
after an expiry or a hard stop.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tennis_engine import __version__
from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.governance.contracts import Principal
from tennis_engine.infrastructure.health import (
    InfrastructureProbe,
    ReadinessProbe,
    public_checks,
    ready,
)
from tennis_engine.infrastructure.settings import Settings

from .auth import TokenAuthenticator
from .contracts import AuditView, MatchAnalysis, RecommendationPage, SourceHealth
from .dashboard import register_dashboard
from .service import ApiError, RecommendationFilter, RecommendationService

CHALLENGE = 'Basic realm="tennis-engine", Bearer'
PROTECTED_PREFIXES = ("/v1/", "/dashboard")


@dataclass(frozen=True)
class Serving:
    """F14 dependencies. Without them every F14 route fails closed with HTTP 503."""

    recommendations: RecommendationService
    authenticator: TokenAuthenticator


def error_response(error: ApiError) -> JSONResponse:
    headers = {"WWW-Authenticate": CHALLENGE} if error.status == 401 else None
    return JSONResponse(
        status_code=error.status,
        content={"error": {"code": error.code, "detail": error.detail}},
        headers=headers,
    )


def create_app(
    settings: Settings | None = None,
    probe: ReadinessProbe | None = None,
    serving: Serving | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_probe = probe or InfrastructureProbe(resolved_settings)
    application = FastAPI(
        title="Tennis Engine",
        version=__version__,
        docs_url=None if resolved_settings.environment == "production" else "/docs",
        redoc_url=None,
    )

    def get_probe() -> ReadinessProbe:
        return resolved_probe

    @application.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(error)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        return error_response(
            ApiError(422, "INVALID_FILTER", f"Invalid request fields: {', '.join(fields)}.")
        )

    @application.middleware("http")
    async def read_only_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(PROTECTED_PREFIXES):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
        return response

    @application.get("/health/live", tags=["health"])
    def liveness() -> dict[str, str]:
        return {"status": "alive", "version": __version__}

    @application.get("/health/ready", tags=["health"])
    def readiness(
        response: Response,
        health_probe: Annotated[ReadinessProbe, Depends(get_probe)],
    ) -> dict[str, object]:
        checks = health_probe.check()
        is_ready = ready(checks)
        if not is_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if is_ready else "not_ready",
            "dependencies": public_checks(checks),
        }

    def get_serving() -> Serving:
        if serving is None:
            raise ApiError(503, "SERVICE_NOT_CONFIGURED", "The recommendation service is off.")
        return serving

    def get_principal(
        dependencies: Annotated[Serving, Depends(get_serving)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        principal = dependencies.authenticator.authenticate(authorization)
        if principal is None:
            raise ApiError(401, "AUTHENTICATION_REQUIRED", "A valid API token is required.")
        return principal

    ServingDep = Annotated[Serving, Depends(get_serving)]
    PrincipalDep = Annotated[Principal, Depends(get_principal)]

    def get_service(dependencies: ServingDep) -> RecommendationService:
        return dependencies.recommendations

    register_dashboard(application, get_service, get_principal)

    @application.get("/v1/tennis/recommendations", tags=["recommendations"])
    def recommendations(
        dependencies: ServingDep,
        principal: PrincipalDep,
        view: Literal["current", "history"] = "current",
        bookmaker: Annotated[str | None, Query(max_length=64)] = None,
        market: Annotated[str | None, Query(max_length=64)] = None,
        decision: Annotated[list[RecommendationStatus] | None, Query()] = None,
        starts_after: datetime | None = None,
        starts_before: datetime | None = None,
        limit: int = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> RecommendationPage:
        return dependencies.recommendations.recommendations(
            principal,
            RecommendationFilter(
                view=view,
                bookmaker=bookmaker,
                market=market,
                statuses=frozenset(decision or ()),
                starts_after=starts_after,
                starts_before=starts_before,
                limit=limit,
                cursor=cursor,
            ),
        )

    @application.get("/v1/tennis/matches/{match_id}/analysis", tags=["recommendations"])
    def analysis(
        match_id: UUID, dependencies: ServingDep, principal: PrincipalDep
    ) -> MatchAnalysis:
        return dependencies.recommendations.analysis(principal, match_id)

    @application.get("/v1/audit/recommendations/{recommendation_id}", tags=["audit"])
    def audit(
        recommendation_id: UUID, dependencies: ServingDep, principal: PrincipalDep
    ) -> AuditView:
        return dependencies.recommendations.audit(principal, recommendation_id)

    @application.get("/v1/tennis/source-health", tags=["recommendations"])
    def health_of_sources(
        dependencies: ServingDep, principal: PrincipalDep
    ) -> tuple[SourceHealth, ...]:
        return dependencies.recommendations.source_health_for(principal)

    return application


app = create_app()
