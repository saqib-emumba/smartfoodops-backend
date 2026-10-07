"""HTTP routes that exist only for the API gateway's own `auth_request` subrequest.

Not part of the public contract — `include_in_schema=False` keeps it off the OpenAPI docs,
and nginx's `internal;` location directive keeps it unreachable from outside the network. See
D51 in readme/key-decisions.md for why the gateway calls this at all, and D57 for why it now
answers 403 as well as 401.
"""

from fastapi import APIRouter, Depends, Header, Response

from common.auth import CurrentUser, verify_access_token
from common.responses import Envelope, ok
from user import policy

router = APIRouter(prefix="/api/v1/users/internal")


@router.get("/verify", response_model=Envelope[None], include_in_schema=False)
def verify_bearer_token(
    response: Response,
    x_original_method: str = Header(..., alias="X-Original-Method"),
    x_original_path: str = Header(..., alias="X-Original-Path"),
    current_user: CurrentUser = Depends(verify_access_token),
) -> Envelope[None]:
    """Authenticate and authorize one request on the gateway's behalf.

    `verify_access_token` raises 401 for anything unusable; `policy.resolve` raises 403 if the
    token is sound but the caller may not have this route. nginx maps both onto the original
    request, so a refusal never reaches a backend service.

    The method and path arrive as headers rather than being read off this request, and they
    have to: nginx issues every `auth_request` subrequest as a `GET` against this fixed path,
    so this handler's own method and URL describe the subrequest, not what the caller asked
    for. Both are required (`...`) — a missing one is a misconfigured gateway, and defaulting
    it would authorize some other route than the one being requested.

    The permission set goes back as a header so the backend service can run its own check
    without a second lookup (D57) — see `require_permission` in services/common/auth.py.
    """
    granted = policy.resolve(x_original_method, x_original_path, current_user.roles)

    response.headers["X-User-Id"] = str(current_user.user_id)
    response.headers["X-User-Roles"] = ",".join(current_user.roles)
    response.headers["X-User-Permissions"] = ",".join(sorted(granted))
    return ok(message="Token is valid")
