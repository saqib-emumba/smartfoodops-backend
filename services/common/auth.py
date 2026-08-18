"""Token issuing and verification — the only module that knows what a token is.

Signing is asymmetric (RS256) on purpose. The User Service is the sole issuer and is the
only container given `JWT_PRIVATE_KEY_B64`; every other service holds nothing but the
public key, so a compromise anywhere else can verify tokens but cannot mint them. Keeping
that split real is why the private key is loaded lazily below rather than at import.

Keys travel base64-encoded in single environment variables because a PEM is multi-line and
`.env` is not, and they are secrets, so they arrive via `required()` and the service refuses
to start without them.

Services also talk to each other. Two mechanisms, deliberately distinct:

    end-user calls    -> the caller's own bearer token, forwarded downstream unchanged, so
                         a service can never do more than the user who invoked it
    internal-only     -> X-Internal-Key, for endpoints no end user should reach directly
                         (see require_internal)
"""

import base64
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from common.config import ACCESS_TOKEN_TTL_MINUTES, required
from common.errors import forbidden, unauthorized

ALGORITHM = "RS256"
ISSUER = "smartfoodops-user-service"
ADMIN_ROLE = "system_admin"


def _decode_key(encoded: str) -> str:
    """Turn a base64 environment variable back into a PEM."""
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - any failure here is fatal misconfiguration
        raise RuntimeError(
            "JWT key is not valid base64. Generate it with: "
            "openssl genrsa 2048 | base64"
        ) from exc


# Every service verifies, so the public key is mandatory everywhere and fails fast.
PUBLIC_KEY = _decode_key(required("JWT_PUBLIC_KEY_B64"))

# Shared by the services that call internal-only endpoints and the ones that expose them.
INTERNAL_API_KEY = required("INTERNAL_API_KEY")

_private_key: str | None = None


def _signing_key() -> str:
    """Load the private key on first use, so only the issuer needs it configured."""
    global _private_key
    if _private_key is None:
        _private_key = _decode_key(required("JWT_PRIVATE_KEY_B64"))
    return _private_key


class Principal(BaseModel):
    """The verified identity behind a request."""

    user_id: UUID
    role: str
    # The raw bearer, kept so route handlers can forward it to downstream services. See
    # the module docstring: downstream calls run as the original caller, never as more.
    token: str

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN_ROLE


def issue_access_token(user_id: UUID, role: str) -> str:
    """Sign a short-lived access token. Only the User Service can call this."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user_id),
        "role": role,
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES),
    }
    return jwt.encode(claims, _signing_key(), algorithm=ALGORITHM)


# auto_error=False so a missing header reaches us as None and becomes a 401 with the
# Bearer challenge; FastAPI's own default would raise a 403, which is the wrong status.
_scheme = HTTPBearer(auto_error=False)


def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_scheme),
) -> Principal:
    """Verify the bearer token and return who is calling. 401 on anything unusable."""
    if credentials is None:
        raise unauthorized("Authorization header with a Bearer token is required")

    token = credentials.credentials
    try:
        claims = jwt.decode(
            token,
            PUBLIC_KEY,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["sub", "role", "exp", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise unauthorized("Access token has expired; refresh it") from exc
    except jwt.InvalidTokenError as exc:
        # Covers bad signatures, wrong issuer and malformed tokens alike. The reason is
        # logged nowhere and never returned: it would tell an attacker which part to fix.
        raise unauthorized("Access token is invalid") from exc

    return Principal(user_id=UUID(claims["sub"]), role=claims["role"], token=token)


def require_role(*allowed: str):
    """Build a dependency admitting only the listed roles.

    Usage: ``principal = Depends(require_role("restaurant_admin"))``. `system_admin` is
    always admitted so an operator is never locked out of an endpoint.
    """

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in allowed and not principal.is_admin:
            raise forbidden(
                f"Role '{principal.role}' may not perform this action "
                f"(requires one of: {', '.join(sorted(allowed))})"
            )
        return principal

    return dependency


def require_self_or_admin(principal: Principal, subject_id: UUID | str) -> None:
    """Guard a resource that only its owner (or an admin) may read.

    Called in the handler body rather than as a dependency because it needs the path
    parameter (or a column just read) to compare against.

    Compared as strings: psycopg2 hands back UUID columns as plain strings unless
    register_uuid() is called, so a caller passing `row["customer_id"]` and one passing a
    parsed path parameter would otherwise never match each other.
    """
    if str(principal.user_id) != str(subject_id) and not principal.is_admin:
        raise forbidden("You may only access your own record")


def require_internal(x_internal_key: str | None = Header(None, alias="X-Internal-Key")):
    """Admit only sibling services, never an end user.

    For endpoints that exist purely as a service-to-service contract. Bearer forwarding
    cannot express this: a forwarded token belongs to the customer who started the request,
    so it would let that customer call the endpoint directly and forge its writes.
    """
    if x_internal_key is None or not secrets.compare_digest(
        x_internal_key, INTERNAL_API_KEY
    ):
        raise unauthorized("This endpoint is internal to SmartFoodOps services")


def bearer(token: str) -> dict:
    """Authorization header forwarding the caller's identity to a sibling service."""
    return {"Authorization": f"Bearer {token}"}


def internal_headers() -> dict:
    """Header proving a request came from a sibling service, not an end user."""
    return {"X-Internal-Key": INTERNAL_API_KEY}


def generate_refresh_token() -> str:
    """A high-entropy opaque token. Opaque, not a JWT, so it can be revoked on logout."""
    return secrets.token_urlsafe(32)
