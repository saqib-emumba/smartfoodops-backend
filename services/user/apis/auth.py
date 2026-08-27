"""HTTP routes for the session lifecycle: register, log in, refresh, log out.

Reading a profile lives in users.py instead — see that module's docstring for why the
split follows what a route answers rather than which repository method it calls.
"""

from fastapi import APIRouter, Depends, status

from common.auth import CurrentUser, get_current_user
from common.errors import unauthorized
from user import deps
from user.schemas.users import (
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserRegisterRequest,
    UserResponse,
)
from user.security import (
    ABSENT_ACCOUNT_HASH,
    INVALID_CREDENTIALS,
    hash_password,
    issue_session,
    verify_password,
)

router = APIRouter(prefix="/api/v1/users")


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_user(payload: UserRegisterRequest) -> UserResponse:
    """Register a user, mapping the incoming role name to roles.id via a DB lookup."""
    row = deps.users.register(payload, hash_password(payload.password))
    return UserResponse(**row)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
    """Exchange credentials for an access token and a refresh session."""
    record = deps.users.find_credentials(str(payload.email))

    # Always run bcrypt, even with no account to check, so both branches cost the same.
    stored_hash = (
        record["password_hash"] if record else ABSENT_ACCOUNT_HASH.decode("utf-8")
    )
    matched = verify_password(payload.password, stored_hash)
    if record is None or not matched:
        deps.logger.info("Failed login attempt for %s", payload.email)
        raise unauthorized(INVALID_CREDENTIALS)

    return issue_session(deps.refresh_tokens, record["id"], record["role"])


@router.post("/refresh", response_model=TokenResponse)
def refresh_session(payload: RefreshRequest) -> TokenResponse:
    """Trade a refresh token for a fresh pair, rotating the refresh token in the process.

    Public by design: the refresh token *is* the credential here, and the access token it
    replaces has usually expired by the time a client calls this.
    """
    user_id = deps.refresh_tokens.consume(payload.refresh_token)
    if user_id is None:
        raise unauthorized("Refresh token is invalid, expired or already used")

    # Re-read the role rather than trusting one captured at login — see find_role.
    record = deps.users.find_role(user_id)
    if record is None:
        raise unauthorized("The account behind this session no longer exists")

    return issue_session(deps.refresh_tokens, user_id, record["role"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    payload: RefreshRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    """End a session by revoking its refresh token.

    The access token already issued stays valid until it expires — that is the trade the
    stateless design makes, and why ACCESS_TOKEN_TTL_MINUTES is short.
    """
    revoked = deps.refresh_tokens.revoke_owned(
        payload.refresh_token, current_user.user_id
    )
    if not revoked:
        deps.logger.info("Logout presented a token that was not live for this user")
