"""HTTP routes for the `session` entity: log in, refresh, log out.

Registering an account lives in users.py instead, beside the rest of the `users` entity's
routes — creating a user is a `users` write, not a session concern, even though it also
opens the first session. See users.py's docstring for the split rationale.
"""

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, get_current_user
from common.errors import unauthorized
from common.responses import Envelope, ok
from user import deps
from user.schemas.sessions import LoginRequest, RefreshRequest, TokenResponse
from user.security import ABSENT_ACCOUNT_HASH, INVALID_CREDENTIALS, issue_session, verify_password

router = APIRouter(prefix="/api/v1/users")


@router.post("/login", response_model=Envelope[TokenResponse])
def login(payload: LoginRequest) -> Envelope[TokenResponse]:
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

    token = issue_session(deps.refresh_tokens, record["id"], record["roles"])
    return ok(token, message="Logged in")


@router.post("/refresh", response_model=Envelope[TokenResponse])
def refresh_session(payload: RefreshRequest) -> Envelope[TokenResponse]:
    """Trade a refresh token for a fresh pair, rotating the refresh token in the process.

    Public by design: the refresh token *is* the credential here, and the access token it
    replaces has usually expired by the time a client calls this.
    """
    user_id = deps.refresh_tokens.consume(payload.refresh_token)
    if user_id is None:
        raise unauthorized("Refresh token is invalid, expired or already used")

    # Re-read the role set rather than trusting one captured at login — see find_roles.
    record = deps.users.find_roles(user_id)
    if record is None:
        raise unauthorized("The account behind this session no longer exists")

    token = issue_session(deps.refresh_tokens, user_id, record["roles"])
    return ok(token, message="Session refreshed")


@router.post("/logout", response_model=Envelope[None])
def logout(
    payload: RefreshRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> Envelope[None]:
    """End a session by revoking its refresh token.

    Answers `200` with a `null` body rather than the platform's old `204`: a `204` response
    has no body by definition, and the envelope needs one to appear in. `POST
    .../picked-up` and `.../delivered` on the Rider Service make the same change, for the
    same reason.

    The access token already issued stays valid until it expires — that is the trade the
    stateless design makes, and why ACCESS_TOKEN_TTL_MINUTES is short.
    """
    revoked = deps.refresh_tokens.revoke_owned(
        payload.refresh_token, current_user.user_id
    )
    if not revoked:
        deps.logger.info("Logout presented a token that was not live for this user")
    return ok(message="Logged out")
