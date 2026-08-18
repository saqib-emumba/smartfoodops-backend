"""SmartFoodOps User Service — registration, login and profile lookups (Port 8001).

Owns its own PostgreSQL database: the `users` and `riders` tables plus the `roles` lookup
that role names resolve against. No other service can read `users` — they hold no
credentials for this database and call GET /api/v1/users/{user_id} instead.

This service is also the system's identity provider: it is the only one given the RS256
private key, so it alone can mint access tokens. Everyone else verifies with the public
key. Sessions hang on refresh tokens in Redis (see tokens.py), which is what makes logout
mean something.
"""

import os
from contextlib import asynccontextmanager
from uuid import UUID

import bcrypt
from fastapi import Depends, FastAPI, status

from common.auth import (
    Principal,
    current_principal,
    generate_refresh_token,
    issue_access_token,
    require_self_or_admin,
)
from common.config import (
    ACCESS_TOKEN_TTL_MINUTES,
    DEFAULT_AUTH_REDIS_URL,
    required,
)
from common.errors import not_found, unauthorized
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from repository import UserRepository
from schemas import (
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserRegisterRequest,
    UserResponse,
)
from tokens import RefreshTokenStore

SERVICE_NAME = "user-service"
DATABASE_URL = required("DATABASE_URL")
AUTH_REDIS_URL = os.getenv("AUTH_REDIS_URL", DEFAULT_AUTH_REDIS_URL)

# One message for every failed login. Saying which half was wrong would turn this endpoint
# into a way to test whether an email address has an account here.
INVALID_CREDENTIALS = "Invalid email or password"

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(DATABASE_URL, logger=logger)
users = UserRepository(db)
refresh_tokens = RefreshTokenStore(AUTH_REDIS_URL, logger=logger)

# Compared against when no account matches, so a login attempt costs the same whether or
# not the email exists. Without it, response time alone answers "is this address
# registered?" — the same question the shared error message above refuses to answer.
_ABSENT_ACCOUNT_HASH = bcrypt.hashpw(b"no-such-account", bcrypt.gensalt())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hold the connection pool and the refresh store open for the whole process."""
    async with db.lifespan(app):
        refresh_tokens.connect()
        try:
            yield
        finally:
            refresh_tokens.close()


app = FastAPI(title="SmartFoodOps User Service", lifespan=lifespan)


def hash_password(plaintext: str) -> str:
    """Hash with a per-password salt; the plaintext is never stored or logged."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, password_hash: str) -> bool:
    return bcrypt.checkpw(plaintext.encode("utf-8"), password_hash.encode("utf-8"))


def _issue_session(user_id: UUID, role: str) -> TokenResponse:
    """Mint an access token and open a refresh session for it."""
    refresh_token = generate_refresh_token()
    refresh_tokens.store(refresh_token, user_id)
    return TokenResponse(
        access_token=issue_access_token(user_id, role),
        refresh_token=refresh_token,
        expires_in=ACCESS_TOKEN_TTL_MINUTES * 60,
    )


@app.get("/api/v1/users/health")
def health():
    """Liveness probe that also proves the database round-trips."""
    return {
        "status": "User Service is up and connected",
        "service": SERVICE_NAME,
        "database_configured": bool(os.getenv("DATABASE_URL")),
        "database_reachable": db.is_reachable(),
        "refresh_store_reachable": refresh_tokens.is_reachable(),
    }


@app.post(
    "/api/v1/users/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_user(payload: UserRegisterRequest) -> UserResponse:
    """Register a user, mapping the incoming role name to roles.id via a DB lookup."""
    row = users.register(payload, hash_password(payload.password))
    return UserResponse(**row)


@app.post("/api/v1/users/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
    """Exchange credentials for an access token and a refresh session."""
    record = users.find_credentials(str(payload.email))

    # Always run bcrypt, even with no account to check, so both branches cost the same.
    stored_hash = (
        record["password_hash"] if record else _ABSENT_ACCOUNT_HASH.decode("utf-8")
    )
    matched = verify_password(payload.password, stored_hash)
    if record is None or not matched:
        logger.info("Failed login attempt for %s", payload.email)
        raise unauthorized(INVALID_CREDENTIALS)

    return _issue_session(record["id"], record["role"])


@app.post("/api/v1/users/refresh", response_model=TokenResponse)
def refresh_session(payload: RefreshRequest) -> TokenResponse:
    """Trade a refresh token for a fresh pair, rotating the refresh token in the process.

    Public by design: the refresh token *is* the credential here, and the access token it
    replaces has usually expired by the time a client calls this.
    """
    user_id = refresh_tokens.consume(payload.refresh_token)
    if user_id is None:
        raise unauthorized("Refresh token is invalid, expired or already used")

    # Re-read the role rather than trusting one captured at login — see find_role.
    record = users.find_role(user_id)
    if record is None:
        raise unauthorized("The account behind this session no longer exists")

    return _issue_session(user_id, record["role"])


@app.post("/api/v1/users/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    payload: RefreshRequest,
    principal: Principal = Depends(current_principal),
) -> None:
    """End a session by revoking its refresh token.

    The access token already issued stays valid until it expires — that is the trade the
    stateless design makes, and why ACCESS_TOKEN_TTL_MINUTES is short.
    """
    revoked = refresh_tokens.revoke_owned(payload.refresh_token, principal.user_id)
    if not revoked:
        logger.info("Logout presented a token that was not live for this user")


@app.get("/api/v1/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: UUID,
    principal: Principal = Depends(current_principal),
) -> UserResponse:
    """Resolve a single profile — the boundary other services read instead of `users`.

    Restricted to the subject and admins. Sibling services reach it while forwarding the
    caller's own token, so their lookups are self-reads and pass the same check: the
    Restaurant Service verifies an owner using that owner's token, the Order Service
    verifies a customer using that customer's.
    """
    require_self_or_admin(principal, user_id)

    row = users.find(user_id)
    if row is None:
        raise not_found(f"User {user_id} not found")
    return UserResponse(**row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
