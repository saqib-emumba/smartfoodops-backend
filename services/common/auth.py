"""Token issuing and verification — the only module that knows what a token is.

Signing is asymmetric (RS256) on purpose. The User Service is the sole issuer and is the
only container given `JWT_PRIVATE_KEY_B64`; every other service holds nothing but the
public key, so a compromise anywhere else can verify tokens but cannot mint them. Keeping
that split real is why the private key is loaded lazily below rather than at import.

Keys travel base64-encoded in single environment variables because a PEM is multi-line and
`.env` is not, and they are secrets, so they arrive via `required()` and the service refuses
to start without them.

D51/D52 moved *where* a token's signature gets checked. `verify_access_token` below is the
only function anywhere that still decodes a JWT — it backs the API gateway's `auth_request`
subrequest (services/user/apis/internal.py) and nothing else. Every other route, in every
service including this one, trusts `X-User-Id`/`X-User-Roles` instead: nginx sets them after
a successful gateway-level verify, and a service calling a sibling directly (off the
gateway's path) sets them itself via `identity_headers()`. See D51 and D52 in
readme/key-decisions.md for the trade-off this accepts — a caller's identity is no longer
backed by a signature once it is past the gateway, only by which container it came from.

D57 moved *what* a caller is allowed to do out of this file entirely. The role literals that
used to sit in `require_role("restaurant_admin")` call sites now live in the User Service's
`permissions`/`role_permissions`/`route_permissions` tables; the gateway's verify subrequest
resolves them per request and passes the answer down as `X-User-Roles` plus
`X-User-Permissions`. What remains here is `require_permission`, which names a capability and
knows nothing about which role holds it. Note what did *not* move: `require_self_or_admin` and
`assert_account_has_role` below still answer questions about a *resource* or a *fetched
account*, which need a row the gateway has never read.

Services also talk to each other. Two mechanisms, deliberately distinct:

    end-user calls    -> the caller's own verified identity, asserted via X-User-Id/
                         X-User-Roles (see identity_headers), so a service can never claim
                         to act as anyone other than whoever called it
    internal-only      -> X-Internal-Key, for endpoints no end user should reach directly
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


# Every service still loads this, even though only verify_access_token (called solely from
# the gateway's verify endpoint, in the User Service) uses it post-D52 — keeping the same
# distribution avoids a docker-compose change for a constant that costs nothing to hold
# unused elsewhere.
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


class CurrentUser(BaseModel):
    """The identity behind a request, trusted rather than re-verified past the gateway."""

    user_id: UUID
    roles: list[str]
    # Resolved from the caller's roles against `role_permissions` by the gateway's verify
    # subrequest (D57), not carried in the token. That is what lets a permission added to a
    # role apply to tokens already in circulation — a *role* grant still needs a refresh
    # (D18), but what a role can do is evaluated fresh on every request.
    #
    # Defaulted empty rather than required: an internal caller asserting identity to a
    # sibling may legitimately have none to pass on, and a route that demands a permission
    # will refuse an empty set anyway. Absence is a denial, never an accidental admit.
    permissions: list[str] = []

    @property
    def is_admin(self) -> bool:
        return ADMIN_ROLE in self.roles

    @property
    def role(self) -> str:
        """Compatibility shim for call sites not yet migrated to `roles`.

        Returns the first granted role. Temporary for the rollout — delete once
        `grep -rn '\\.role\\b' services/` (excluding `.roles`) comes back empty.
        """
        return self.roles[0]


def issue_access_token(user_id: UUID, roles: list[str]) -> str:
    """Sign a short-lived access token. Only the User Service can call this."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user_id),
        "roles": roles,
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES),
    }
    return jwt.encode(claims, _signing_key(), algorithm=ALGORITHM)


# auto_error=False so a missing header reaches us as None and becomes a 401 with the
# Bearer challenge; FastAPI's own default would raise a 403, which is the wrong status.
_scheme = HTTPBearer(auto_error=False)


def verify_access_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_scheme),
) -> CurrentUser:
    """Cryptographically verify a bearer token. 401 on anything unusable.

    The only function left anywhere that checks a JWT signature (D51/D52). Called solely by
    the gateway's own verify endpoint (services/user/apis/internal.py), reached through
    nginx's `auth_request` before any other route runs. Every other route in every service,
    including this one's own public routes, depends on `get_current_user` below instead.
    """
    if credentials is None:
        raise unauthorized("Authorization header with a Bearer token is required")

    token = credentials.credentials
    try:
        claims = jwt.decode(
            token,
            PUBLIC_KEY,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["sub", "roles", "exp", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise unauthorized("Access token has expired; refresh it") from exc
    except jwt.InvalidTokenError as exc:
        # Covers bad signatures, wrong issuer and malformed tokens alike. The reason is
        # logged nowhere and never returned: it would tell an attacker which part to fix.
        raise unauthorized("Access token is invalid") from exc

    return CurrentUser(user_id=UUID(claims["sub"]), roles=claims["roles"])


def get_current_user(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    x_user_roles: str | None = Header(None, alias="X-User-Roles"),
    x_user_permissions: str | None = Header(None, alias="X-User-Permissions"),
) -> CurrentUser:
    """The identity behind a request, trusted rather than re-verified (D51/D52/D57).

    A gateway-fronted request has these headers set by nginx, after `auth_request` calls
    `verify_access_token` and the token checks out. An internal, service-to-service request
    has them set by the caller via `identity_headers()`, re-asserting the identity it already
    had verified for its own inbound request. Either way, this dependency does not itself
    check a signature — something upstream of it already did, or should have.

    `X-User-Permissions` is the capability set the gateway resolved from the caller's roles
    against the policy tables (D57). Missing is not fatal here, unlike the other two: the
    permission checks downstream treat an empty set as a denial, so a caller arriving without
    it can reach an ownership-gated route and nothing more.
    """
    if not x_user_id or not x_user_roles:
        raise unauthorized(
            "Missing verified identity — this route must be reached through the gateway"
        )
    try:
        user_id = UUID(x_user_id)
    except ValueError as exc:
        raise unauthorized("Invalid identity header") from exc

    return CurrentUser(
        user_id=user_id,
        roles=[r for r in x_user_roles.split(",") if r],
        permissions=[p for p in (x_user_permissions or "").split(",") if p],
    )


def require_permission(*allowed: str):
    """Build a dependency admitting callers who hold at least one of the listed permissions.

    Usage: ``current_user = Depends(require_permission("menu:write"))``. `system_admin` is
    always admitted so an operator is never locked out of an endpoint.

    Replaces `require_role` (D57). A handler names the *capability* it needs, never the role
    that happens to hold it — which role that is lives in `role_permissions` in the User
    Service database, so granting `menu:write` to a new role is a SQL change and no service
    is rebuilt. The role literals that used to sit at these call sites are gone.

    This is the second of two enforcement points, not the only one: the gateway already
    refused this request if the caller lacked the permission (services/user/policy.py), and
    this check is what still stands when a sibling service is reached directly, off the
    gateway's path, where `X-User-Roles` is asserted by the caller rather than verified
    (D52). Removing it would turn network reachability into privilege escalation.

    The message names the required permission rather than the caller's roles: a caller
    cannot act on "you are not a restaurant_admin", and saying so leaks how the platform's
    roles are arranged to anyone probing endpoints.
    """

    def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not (set(current_user.permissions) & set(allowed)) and not current_user.is_admin:
            raise forbidden(
                "You do not hold the permission required for this action "
                f"(requires one of: {', '.join(sorted(allowed))})"
            )
        return current_user

    return dependency


def require_self_or_admin(
    current_user: CurrentUser,
    subject_id: UUID | str,
    *,
    detail: str = "You may only access your own record",
) -> None:
    """Guard a resource that only its owner (or an admin) may read.

    Called in the handler body rather than as a dependency because it needs the path
    parameter (or a column just read) to compare against.

    Compared as strings: psycopg2 hands back UUID columns as plain strings unless
    register_uuid() is called, so a caller passing `row["customer_id"]` and one passing a
    parsed path parameter would otherwise never match each other.

    `detail` exists so ownership rules that are not about *your own record* — owning the
    restaurant an order was placed with, for instance — can reuse this comparison instead of
    hand-rolling it and getting the admin bypass wrong.
    """
    if str(current_user.user_id) != str(subject_id) and not current_user.is_admin:
        raise forbidden(detail)


def assert_account_has_role(account: dict, *, required: str, detail: str) -> dict:
    """Assert a fetched account currently holds a role, independent of its token.

    Three services do this after reading `GET /api/v1/users/{id}`, and the reason is the
    same in all three: the role claim in a token was true when the token was signed, so an
    account demoted since then still presents a valid one until it expires (D18).

    The comparison is shared; the route, the role literal and the message are not. Which URL
    produced this dict and how a rejection is worded belong to the calling service — those
    messages are distinct response bodies, not boilerplate.
    """
    if required not in account.get("roles", []):
        raise forbidden(detail)
    return account


def require_internal(x_internal_key: str | None = Header(None, alias="X-Internal-Key")):
    """Admit only sibling services, never an end user.

    For endpoints that exist purely as a service-to-service contract. A caller's own
    identity headers cannot express this: they belong to the customer who started the
    request, so honouring them here would let that customer call the endpoint directly and
    forge its writes.
    """
    if x_internal_key is None or not secrets.compare_digest(
        x_internal_key, INTERNAL_API_KEY
    ):
        raise unauthorized("This endpoint is internal to SmartFoodOps services")


def identity_headers(current_user: CurrentUser) -> dict:
    """Header pair asserting the caller's identity to a sibling service (D15, D51/D52).

    Replaces forwarding the raw bearer token: an internal call never passes through the
    gateway's `auth_request` check, so the calling service re-asserts the identity it already
    had verified for its own inbound request — the same thing nginx does for gateway-fronted
    calls. A service can still never act as anyone other than whoever called it; it can only
    ever assert the identity it was itself given, never invent one.
    """
    return {
        "X-User-Id": str(current_user.user_id),
        "X-User-Roles": ",".join(current_user.roles),
        # The capability set this caller was handed, forwarded unchanged (D57). Resolved
        # rather than re-derived: a sibling service holds no policy tables and must not have
        # to, so it can only ever pass on what the gateway already decided — the same rule
        # the two headers above follow.
        "X-User-Permissions": ",".join(current_user.permissions),
    }


def internal_headers() -> dict:
    """Header proving a request came from a sibling service, not an end user."""
    return {"X-Internal-Key": INTERNAL_API_KEY}


def generate_refresh_token() -> str:
    """A high-entropy opaque token. Opaque, not a JWT, so it can be revoked on logout."""
    return secrets.token_urlsafe(32)
