"""The rider's own account, verified against the User Service."""

from uuid import UUID

from common.auth import assert_account_role, bearer
from common.config import DEFAULT_USER_SERVICE_URL
from common.service_client import ServiceFacade

# Only this role may join the delivery fleet.
RIDER_ROLE = "rider"


class UserServiceClient(ServiceFacade):
    display_name = "User Service"
    env_var = "USER_SERVICE_URL"
    default_url = DEFAULT_USER_SERVICE_URL

    def verify_rider(self, user_id: UUID, token: str) -> dict:
        """Confirm the account exists and may currently ride.

        Looks redundant now that the token carries a role, and is not: that claim was true
        when the token was signed. An account demoted since then still presents a valid
        token until it expires, and only this lookup notices (D18).
        """
        account = self._client.get(
            f"/api/v1/users/{user_id}",
            missing=f"Account {user_id} does not exist",
            unreachable_hint="cannot verify the rider account",
            bad_gateway_hint="verifying the rider account",
            headers=bearer(token),
        )
        return assert_account_role(
            account,
            required=RIDER_ROLE,
            detail=(
                f"Account {user_id} has role '{account.get('role')}' and is not authorised "
                f"to join the delivery fleet (requires '{RIDER_ROLE}')"
            ),
        )
