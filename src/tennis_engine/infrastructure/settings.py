"""Typed environment configuration; secrets remain wrapped and excluded from output."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @model_validator(mode="after")
    def production_has_real_secrets(self) -> "Settings":
        if self.environment == "production":
            insecure = {"local-development", "change-me-before-use", "change-me"}
            values = {
                self.object_store_access_key.get_secret_value(),
                self.object_store_secret_key.get_secret_value(),
            }
            if values & insecure:
                raise ValueError("Production requires externally supplied object-store credentials")
            if not self.object_store_secure:
                raise ValueError("Production object storage must use TLS")
        return self

    def public_summary(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "database_driver": self.database_url.split(":", 1)[0],
            "object_store_endpoint": self.object_store_endpoint,
            "object_store_bucket": self.object_store_bucket,
            "object_store_secure": self.object_store_secure,
            "artifact_root": str(self.artifact_root),
        }
