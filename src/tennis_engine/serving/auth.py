"""F14.2 token authentication and role permissions, enforced on the server.

The server stores only SHA-256 digests of API tokens. A request sends the token as
`Authorization: Bearer <token>` or as the password of HTTP Basic authentication, so a
browser can open the dashboard. Tokens in query strings are not accepted.
Every F14 route is read-only. No role can change identities, policy, funds, models or risk.
"""

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from pydantic import TypeAdapter

from tennis_engine.common.contracts import Contract, Digest
from tennis_engine.governance.contracts import Principal, Role, Text


class Permission(StrEnum):
    READ_RECOMMENDATIONS = "read_recommendations"
    READ_ANALYSIS = "read_analysis"
    READ_AUDIT = "read_audit"
    # See source values even when the source does not permit redistribution.
    READ_RESTRICTED_SOURCE_VALUES = "read_restricted_source_values"


VIEWER = frozenset({Permission.READ_RECOMMENDATIONS, Permission.READ_ANALYSIS})
INTERNAL = frozenset(Permission)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.DASHBOARD: VIEWER,
    Role.AGENT: VIEWER,
    Role.OPERATOR: INTERNAL,
    Role.POLICY_REVIEWER: INTERNAL,
}


def allowed(principal: Principal, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(principal.role, frozenset())


class ApiCredential(Contract):
    identity: Text
    role: Role
    token_sha256: Digest


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_credentials(path: Path) -> tuple[ApiCredential, ...]:
    """Read a JSON list of credentials. The file holds digests, never plain tokens."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return TypeAdapter(tuple[ApiCredential, ...]).validate_python(data)


class TokenAuthenticator:
    def __init__(self, credentials: Iterable[ApiCredential]) -> None:
        self._credentials = tuple(credentials)
        digests = [item.token_sha256 for item in self._credentials]
        if len(set(digests)) != len(digests):
            raise ValueError("Each API token must be unique")

    def authenticate(self, authorization: str | None) -> Principal | None:
        token = _token(authorization)
        if token is None:
            return None
        offered = token_digest(token)
        found: ApiCredential | None = None
        # Compare with every credential so timing does not show which entry matched.
        for item in self._credentials:
            if hmac.compare_digest(offered, item.token_sha256):
                found = item
        if found is None:
            return None
        return Principal(identity=found.identity, role=found.role)


def _token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    value = value.strip()
    if not value:
        return None
    if scheme.lower() == "bearer":
        return value
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        _, separator, password = decoded.partition(":")
        return password if separator and password else None
    return None
