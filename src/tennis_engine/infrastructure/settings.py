"""Typed environment configuration; secrets remain wrapped and excluded from output."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

# Values from `.env.example`, Compose defaults and documentation. Production refuses them.
PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "change-me",
        "change-me-before-use",
        "local-development",
        "local-only",
        "tennis",
        "test-only",
        "password",
    }
)


def _database_password(url: str) -> str | None:
    try:
        return make_url(url).password
    except ArgumentError:
        return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TENNIS_",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://tennis:tennis@localhost:5432/tennis"
    object_store_endpoint: str = "localhost:9000"
    object_store_access_key: SecretStr = SecretStr("local-development")
    object_store_secret_key: SecretStr = SecretStr("change-me-before-use")
    object_store_bucket: str = "tennis-raw"
    object_store_secure: bool = False
    artifact_root: Path = Path("var/artifacts")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    worker_poll_seconds: float = Field(default=5.0, gt=0, le=300)

    # F14 serving. Off by default: every F14 route then returns HTTP 503.
    serving_enabled: bool = False
    # JSON list of token digests (see `tennis-platform create-api-token`).
    api_credentials_file: Path | None = None
    # F01 governance journal. The API opens it read-only.
    governance_journal: Path | None = None
    # Optional read-only database role for the API. Default: `database_url`.
    serving_database_url: SecretStr | None = None
    serving_account_scope: str = Field(default="shadow", min_length=1, max_length=64)
    serving_stale_after_seconds: int = Field(default=300, gt=0, le=86_400)
    # A database outage then gives HTTP 503 quickly instead of a hung request.
    serving_connect_timeout_seconds: int = Field(default=3, gt=0, le=60)

    @model_validator(mode="after")
    def production_has_real_secrets(self) -> "Settings":
        if self.environment == "production":
            values = {
                self.object_store_access_key.get_secret_value(),
                self.object_store_secret_key.get_secret_value(),
            }
            if values & PLACEHOLDER_SECRETS:
                raise ValueError("Production requires externally supplied object-store credentials")
            if not self.object_store_secure:
                raise ValueError("Production object storage must use TLS")
            urls = [self.database_url]
            if self.serving_database_url is not None:
                urls.append(self.serving_database_url.get_secret_value())
            for url in urls:
                password = _database_password(url)
                if password is None or password in PLACEHOLDER_SECRETS:
                    raise ValueError("Production requires an externally supplied database password")
        if self.serving_enabled:
            if self.api_credentials_file is None:
                raise ValueError("Serving requires TENNIS_API_CREDENTIALS_FILE")
            if self.governance_journal is None:
                raise ValueError("Serving requires TENNIS_GOVERNANCE_JOURNAL")
        return self

    def serving_database(self) -> str:
        if self.serving_database_url is not None:
            return self.serving_database_url.get_secret_value()
        return self.database_url

    def public_summary(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "database_driver": self.database_url.split(":", 1)[0],
            "object_store_endpoint": self.object_store_endpoint,
            "object_store_bucket": self.object_store_bucket,
            "object_store_secure": self.object_store_secure,
            "artifact_root": str(self.artifact_root),
            "serving_enabled": self.serving_enabled,
            "serving_database_role": "separate" if self.serving_database_url else "shared",
            # Paths can name a local user directory, so show only whether they are set.
            "api_credentials_file": "set" if self.api_credentials_file else "unset",
            "governance_journal": "set" if self.governance_journal else "unset",
        }
