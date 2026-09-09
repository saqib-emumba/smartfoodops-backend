"""HTTP routes for the `users` resource: registration and reading a profile.

The health probe lives in health.py instead — see that module's docstring. Registration
lives here rather than in sessions.py because it is a `users` write — it inserts a row in
the table this file's other route reads — even though it also opens the account's first
session as a side effect (see `issue_session` inside `login`, which `register` does not
call: a new account signs in separately, matching D13's rule that identity is never assumed
from a request that just created it).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from common.auth import CurrentUser, get_current_user, require_internal, require_self_or_admin
from common.errors import not_found
from common.responses import Envelope, ok
from user import deps
from user.schemas.users import UserRegisterRequest, UserResponse
from user.security import hash_password

router = APIRouter(prefix="/api/v1/users")


@router.post(
    "/register", response_model=Envelope[UserResponse], status_code=status.HTTP_201_CREATED
)
def register_user(payload: UserRegisterRequest) -> Envelope[UserResponse]:
    """Register a user, mapping the incoming role name to roles.id via a DB lookup."""
    row = deps.users.register(payload, hash_password(payload.password))
    return ok(UserResponse(**row), message="User registered", status=201)


@router.get("/{user_id}", response_model=Envelope[UserResponse])
def get_user(
    user_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> Envelope[UserResponse]:
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
    return ok(UserResponse(**row), message="User found")


@router.get(
    "/{user_id}/internal",
    response_model=Envelope[UserResponse],
    dependencies=[Depends(require_internal)],
)
def get_user_internally(user_id: UUID) -> Envelope[UserResponse]:
    """The same profile as the bearer-token route above, for a caller with no user token
    to forward (Week 3, D45) — the Analytics/Notification path's equivalent of
    `order/apis/checkout.py::get_order_internally`, added for D26's same reason: a Kafka
    consumer has no request, and therefore no bearer token, behind it at all.

    Internal-key only, because without the token there is no ownership check left here —
    this must not be reachable by anyone who could guess a user id, the same caveat
    `get_order_internally`'s own docstring already states.
    """
    row = deps.users.find(user_id)
    if row is None:
        raise not_found(f"User {user_id} not found")
    return ok(UserResponse(**row), message="User found")
