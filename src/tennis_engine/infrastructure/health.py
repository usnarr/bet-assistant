"""Dependency probes used by API readiness and the worker startup gate."""

from dataclasses import dataclass
from typing import Protocol

from minio import Minio

from .database import build_engine, database_ready
from .settings import Settings


@dataclass(frozen=True)
class Check:
    ready: bool
    detail: str


class ReadinessProbe(Protocol):
    def check(self) -> dict[str, Check]: ...


class InfrastructureProbe:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check(self) -> dict[str, Check]:
        engine = build_engine(self.settings.database_url)
        try:
            database_ok, database_detail = database_ready(engine)
        finally:
            engine.dispose()
        try:
            client = Minio(
                self.settings.object_store_endpoint,
                access_key=self.settings.object_store_access_key.get_secret_value(),
                secret_key=self.settings.object_store_secret_key.get_secret_value(),
                secure=self.settings.object_store_secure,
            )
            object_store_ok = client.bucket_exists(self.settings.object_store_bucket)
            object_store_detail = (
                self.settings.object_store_bucket
                if object_store_ok
                else "required bucket is missing"
            )
        except Exception as error:
            object_store_ok = False
            object_store_detail = f"{type(error).__name__}: object store unavailable"
        return {
            "database": Check(database_ok, database_detail),
            "object_store": Check(object_store_ok, object_store_detail),
        }


def ready(checks: dict[str, Check]) -> bool:
    return bool(checks) and all(check.ready for check in checks.values())


def public_checks(checks: dict[str, Check]) -> dict[str, dict[str, object]]:
    return {
        name: {"ready": check.ready, "detail": check.detail}
        for name, check in sorted(checks.items())
    }
