import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tennis_engine.infrastructure.cli import main
from tennis_engine.infrastructure.health import Check
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.serving.api import create_app


class Probe:
    def __init__(self, state: bool):
        self.state = state

    def check(self):
        return {
            "database": Check(self.state, "0002_governance" if self.state else "unavailable"),
            "object_store": Check(self.state, "tennis-raw" if self.state else "unavailable"),
        }


def test_health_endpoints_separate_liveness_from_readiness():
    settings = Settings(environment="test")
    ready_client = TestClient(create_app(settings, Probe(True)))
    assert ready_client.get("/health/live").status_code == 200
    response = ready_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"

    unavailable_client = TestClient(create_app(settings, Probe(False)))
    assert unavailable_client.get("/health/live").status_code == 200
    response = unavailable_client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_settings_never_include_secret_values_in_public_summary(capsys):
    settings = Settings(
        environment="test",
        object_store_access_key="visible-only-to-client",
        object_store_secret_key="never-print-this",
    )
    rendered = json.dumps(settings.public_summary())
    assert "visible-only-to-client" not in rendered
    assert "never-print-this" not in rendered
    assert main(["show-config"]) == 0
    assert "secret" not in capsys.readouterr().out.lower()


def test_production_rejects_placeholders_and_insecure_object_storage():
    with pytest.raises(ValidationError, match="externally supplied"):
        Settings(environment="production")
    with pytest.raises(ValidationError, match="TLS"):
        Settings(
            environment="production",
            object_store_access_key="production-access",
            object_store_secret_key="production-secret",
            object_store_secure=False,
        )
