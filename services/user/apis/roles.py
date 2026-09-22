"""HTTP routes for granting and revoking roles on an existing account.

Registration (users.py) grants exactly one role at signup. Everything after that — a
restaurant_admin who also wants to place orders as a customer, for instance — goes through
here instead. No separate GET: a user's role set is already part of the profile returned by
`GET /api/v1/users/{id}` (UserResponse.roles), so a parallel read path here would be redundant.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from common.auth import ADMIN_ROLE, CurrentUser, get_current_user, require_self_or_admin
from common.errors import forbidden
from common.responses import Envelope, ok
from user import deps
from user.schemas.users import RoleGrantRequest

router = APIRouter(prefix="/api/v1/users")


def _guard_role_change(current_user: CurrentUser, user_id: UUID, role_name: str) -> None:
    """Ownership, plus the one carve-out: touching `system_admin` needs to already be one.

    Every other role is self-or-admin, same as the rest of this service's routes — a user
    may grant or revoke `customer`/`restaurant_admin`/`rider` on their own account freely.
    `system_admin` is different in kind: unlike those three, it bypasses every ownership
    check in the platform (`require_role`, `require_self_or_admin`), so holding the label
    already *is* the access, with nothing further gating a specific resource underneath. That
    makes it the one role where self-service would let anyone opt into bypassing every check
    that would otherwise apply to them — so granting or revoking it, on any account including
    the caller's own, requires the caller to already be a `system_admin`.
    """
    require_self_or_admin(current_user, user_id)
    if role_name == ADMIN_ROLE and not current_user.is_admin:
        raise forbidden("Only a system_admin may grant or revoke the system_admin role")


@router.post(
    "/{user_id}/roles", response_model=Envelope[None], status_code=status.HTTP_200_OK
)
def grant_role(
    user_id: UUID,
    payload: RoleGrantRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> Envelope[None]:
    """Add a role to a user's account. Idempotent — granting an already-held role is a no-op."""
    _guard_role_change(current_user, user_id, payload.role)
    deps.users.grant_role(user_id, payload.role)
    return ok(message=f"Role '{payload.role}' granted")


@router.delete("/{user_id}/roles/{role_name}", response_model=Envelope[None])
def revoke_role(
    user_id: UUID,
    role_name: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> Envelope[None]:
    """Remove a role from a user's account. Refuses to leave the account with zero roles."""
    _guard_role_change(current_user, user_id, role_name)
    deps.users.revoke_role(user_id, role_name)
    return ok(message=f"Role '{role_name}' revoked")
