"""The session entity: what it takes to log in, refresh, and what a token pair looks like.

Split out of schemas/users.py, which held three distinct concerns (user, role enum, session)
in one file. `UserRegisterRequest`/`UserResponse` describe the `users` table; nothing here
does — a session is a Redis-backed refresh token (see repositories/sessions.py), not a row.
"""

from pydantic import BaseModel, Field
from pydantic import EmailStr


class LoginRequest(BaseModel):
    # Neither field is length-constrained, unlike registration: a 422 on a too-short
    # password would answer "that is not our password format" before checking anything,
    # and every rejected login must look identical. See apis/sessions.py:login.
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Access token lifetime in seconds")
