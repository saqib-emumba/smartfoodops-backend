"""Owner identity and authorisation, verified against the User Service.

The lookup runs as the owner, forwarding their bearer token, so this service can never
read more than they could (D15).

This service used to relay kitchen decisions into the order saga on the internal key. D32
removed that: the decision is recorded by the Order Service, which owns both the column and
the workflow, so there is no relay here to lose and this service holds no internal
credential at all.
"""

from uuid import UUID

from common.auth import assert_account_role, bearer
from common.config import DEFAULT_USER_SERVICE_URL
from common.service_client import ServiceFacade

# Only this role may onboard restaurants. Stays here, not in common: which role a route
# demands is this service's policy, not the chassis's.
OWNER_ROLE = "restaurant_admin"


class UserServiceClient(ServiceFacade):
    display_name = "User Service"
    env_var = "USER_SERVICE_URL"
    default_url = DEFAULT_USER_SERVICE_URL

    def verify_owner(self, owner_id: UUID, token: str) -> dict:
        """Confirm the owner exists and may onboard restaurants.

        Looks redundant now that the access token carries a role, and is not: that claim
        was true when the token was signed. An account demoted since then still presents a
        valid token until it expires, and only this lookup notices.
        """
        owner = self._client.get(
            f"/api/v1/users/{owner_id}",
            missing=f"Owner {owner_id} does not exist",
            unreachable_hint="cannot verify restaurant owner",
            bad_gateway_hint="verifying owner",
            headers=bearer(token),
        )
        return assert_account_role(
            owner,
            required=OWNER_ROLE,
            detail=(
                f"Owner {owner_id} has role '{owner.get('role')}' and is not authorised "
                f"to onboard restaurants (requires '{OWNER_ROLE}')"
            ),
        )
