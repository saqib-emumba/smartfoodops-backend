"""The customer an order names, verified against the User Service."""

from uuid import UUID

from common.auth import bearer
from common.config import DEFAULT_USER_SERVICE_URL
from common.errors import unprocessable
from common.service_client import ServiceFacade


class UserServiceClient(ServiceFacade):
    display_name = "User Service"
    env_var = "USER_SERVICE_URL"
    default_url = DEFAULT_USER_SERVICE_URL

    def verify_customer(self, customer_id: UUID, token: str) -> dict:
        """Confirm the customer exists — the check the `customer_id` foreign key made.

        `customer_id` now comes from the token being forwarded, so this is a self-read and
        satisfies the User Service's own self-or-admin rule.
        """
        return self._client.get(
            f"/api/v1/users/{customer_id}",
            missing=f"Unknown customer {customer_id} referenced by this order",
            missing_error=unprocessable,
            unreachable_hint="cannot verify the customer",
            bad_gateway_hint="verifying the customer",
            headers=bearer(token),
        )
