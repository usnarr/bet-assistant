"""Stable request identities that never retain common credential parameters."""

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

SENSITIVE_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "key",
        "password",
        "secret",
        "signature",
        "sig",
        "token",
    }
)


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if parts.port is not None:
        hostname = f"{hostname}:{parts.port}"
    query = urlencode(
        [
            (name, "[REDACTED]" if name.casefold() in SENSITIVE_QUERY_NAMES else value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        doseq=True,
        quote_via=quote,
        safe="[]",
    )
    return urlunsplit((parts.scheme, hostname, parts.path, query, ""))


def request_identity(method: str, url: str) -> str:
    normalized_method = method.strip().upper()
    if not normalized_method:
        raise ValueError("HTTP method is required")
    return f"{normalized_method} {redact_url(url)}"
