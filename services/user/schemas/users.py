"""Pydantic v2 validation schemas for the `users` entity.

The session entity — login, refresh, and the token pair they produce — lives in
schemas/sessions.py instead: a session is not a row in this table, it is a Redis-backed
refresh token, and bundling it in here was the original flat-file layout's leftover.
"""

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRole(str, Enum):
    """Roles seeded into the `roles` lookup table by db/user/init.sql."""

    customer = "customer"
    restaurant_admin = "restaurant_admin"
    rider = "rider"
    system_admin = "system_admin"


class UserRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)  # bcrypt hashes at most 72 bytes
    full_name: str = Field(..., min_length=2)
    phone: str = Field(..., min_length=8)
    # Kept as a plain string (not the UserRole enum) so that the `roles` table stays the single
    # source of truth and an unknown role can be rejected with 400 Bad Request rather than 422.
    role: str = Field(UserRole.customer.value, min_length=2)


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    phone: str
    role: str  # Resolves database roles table lookup via SQL Join query on role_id

    model_config = ConfigDict(from_attributes=True)
