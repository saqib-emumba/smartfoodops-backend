"""HTTP routes for the `users` resource: the health probe and reading a profile.

Session lifecycle (register, login, refresh, logout) lives in auth.py instead — the two
files split on what a route answers, not on which repository method it calls: every route
here and there reads or writes through `deps.users`, but "give me a session" and "read a
profile" are different questions to a caller.
"""

import os
from uuid import UUID

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, get_current_user, require_self_or_admin
from common.errors import not_found
from common.health import health_payload
from user import deps
from user.schemas.users import UserResponse

router = APIRouter(prefix="/api/v1/users")


@router.get("/health")
def health():
    """Liveness probe that also proves the database round-trips."""
    return health_payload(
        deps.SERVICE_NAME,
        deps.db,
        status="User Service is up and connected",
        database_configured=bool(os.getenv("DATABASE_URL")),
        refresh_store_reachable=deps.refresh_tokens.is_reachable(),
    )


@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> UserResponse:
    """Resolve a single profile — the boundary other services read instead of `users`.

    Restricted to the subject and admins. Sibling services reach it while forwarding the
    caller's own token, so their lookups are self-reads and pass the same check: the
    Restaurant Service verifies an owner using that owner's token, the Order Service
    verifies a customer using that customer's.
    """
    require_self_or_admin(current_user, user_id)

    row = deps.users.find(user_id)
    if row is None:
        raise not_found(f"User {user_id} not found")
    return UserResponse(**row)
