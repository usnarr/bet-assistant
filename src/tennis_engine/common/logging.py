"""JSON logging helpers with conservative credential redaction."""

import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SECRET_MARKERS = ("authorization", "cookie", "password", "secret", "token", "api_key", "access_key")
REDACTED = "[REDACTED]"


def sanitize(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in SECRET_MARKERS):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(item_key): sanitize(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parts = urlsplit(value)
        if parts.query:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, REDACTED, parts.fragment))
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if context is not None:
            payload["context"] = sanitize(context)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
