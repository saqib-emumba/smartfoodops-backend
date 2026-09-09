"""Resolving a customer's contact details, against the User Service.

The event a notification reacts to carries `customer_id`, never a phone number or an email
address — putting contact details in a Kafka message that everything subscribed to the
topic can read would be a real data-handling regression the platform has not made anywhere
else. This client is what looks the actual contact details up, on the internal key, the
moment they're needed and nowhere they're kept.
"""

from uuid import UUID

from common.auth import internal_headers
from common.config import DEFAULT_USER_SERVICE_URL
from common.errors import not_found
from common.service_client import ServiceFacade


class UserServiceClient(ServiceFacade):
    display_name = "User Service"
    env_var = "USER_SERVICE_URL"
    default_url = DEFAULT_USER_SERVICE_URL

    async def fetch_contact_details(self, user_id: UUID | str) -> dict:
        """Read a profile on the internal key — this consumer has no user token behind it
        at all, the same reasoning D26 already gave the saga's activities: a Kafka consumer
        is not a request, so there is no bearer token to forward (Week 3, D45).
        """
        return await self._client.aget(
            f"/api/v1/users/{user_id}/internal",
            missing=f"Unknown user {user_id} referenced by this event",
            missing_error=not_found,
            unreachable_hint="cannot resolve contact details",
            bad_gateway_hint="resolving contact details",
            headers=internal_headers(),
        )
