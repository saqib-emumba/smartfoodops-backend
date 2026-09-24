"""HTTP routes that exist only for the API gateway's own `auth_request` subrequest.

Not part of the public contract — `include_in_schema=False` keeps it off the OpenAPI docs,
and nginx's `internal;` location directive keeps it unreachable from outside the network. See
D51 in readme/key-decisions.md for why the gateway calls this at all.
"""

from fastapi import APIRouter, Depends, Response

from common.auth import CurrentUser, verify_access_token
from common.responses import Envelope, ok

router = APIRouter(prefix="/api/v1/users/internal")


@router.get("/verify", response_model=Envelope[None], include_in_schema=False)
def verify_bearer_token(
    response: Response,
    current_user: CurrentUser = Depends(verify_access_token),
) -> Envelope[None]:
    """Answer 200 with identity headers for a valid token; verify_access_token raises 401 otherwise."""
    response.headers["X-User-Id"] = str(current_user.user_id)
    response.headers["X-User-Roles"] = ",".join(current_user.roles)
    return ok(message="Token is valid")
