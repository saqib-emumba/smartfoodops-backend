"""Password hashing and session minting for the User Service.

Separated from the routes because two of these are security mechanisms with reasons that
have nothing to do with HTTP: the constant-time login cost, and the fact that an access
token and a refresh session are always opened together.
"""

from uuid import UUID

import bcrypt

from common.auth import generate_refresh_token, issue_access_token
from common.config import ACCESS_TOKEN_TTL_MINUTES
from user.schemas.sessions import TokenResponse

# One message for every failed login. Saying which half was wrong would turn the login
# endpoint into a way to test whether an email address has an account here.
INVALID_CREDENTIALS = "Invalid email or password"

# Compared against when no account matches, so a login attempt costs the same whether or
# not the email exists. Without it, response time alone answers "is this address
# registered?" — the same question the shared error message above refuses to answer.
#
# Computed once at import. This is the one bcrypt call the service makes outside a request,
# and it is why this module must not be imported from anywhere hot.
ABSENT_ACCOUNT_HASH = bcrypt.hashpw(b"no-such-account", bcrypt.gensalt())


def hash_password(plaintext: str) -> str:
    """Hash with a per-password salt; the plaintext is never stored or logged."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, password_hash: str) -> bool:
    return bcrypt.checkpw(plaintext.encode("utf-8"), password_hash.encode("utf-8"))


def issue_session(store, user_id: UUID, role: str) -> TokenResponse:
    """Mint an access token and open a refresh session for it.

    `store` is the RefreshTokenStore, passed in rather than imported, so this module stays
    free of the service's wiring and can be reasoned about on its own.
    """
    refresh_token = generate_refresh_token()
    store.store(refresh_token, user_id)
    return TokenResponse(
        access_token=issue_access_token(user_id, role),
        refresh_token=refresh_token,
        expires_in=ACCESS_TOKEN_TTL_MINUTES * 60,
    )
