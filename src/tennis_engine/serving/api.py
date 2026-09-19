"""Minimal internal API with separate liveness and dependency readiness."""

from typing import Annotated

from fastapi import Depends, FastAPI, Response, status

from tennis_engine import __version__
from tennis_engine.infrastructure.health import (
    InfrastructureProbe,
    ReadinessProbe,
    public_checks,
    ready,
)
from tennis_engine.infrastructure.settings import Settings


def create_app(settings: Settings | None = None, probe: ReadinessProbe | None = None) -> FastAPI:
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

    return application


app = create_app()
