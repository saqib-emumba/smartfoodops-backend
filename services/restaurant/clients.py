"""Outbound calls to sibling services.

Owner identity and authorisation live in the User Service, so they are resolved over HTTP
rather than by reading the `users` table.
"""

import os
from logging import Logger
from uuid import UUID

from common.config import DEFAULT_USER_SERVICE_URL
from common.errors import forbidden
from common.service_client import ServiceClient

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", DEFAULT_USER_SERVICE_URL)

# Only this role may onboard restaurants.
OWNER_ROLE = "restaurant_admin"


class UserServiceClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient("User Service", USER_SERVICE_URL, logger=logger)

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def verify_owner(self, owner_id: UUID) -> dict:
        """Confirm the owner exists and may onboard restaurants."""
        owner = self._client.get(
            f"/api/v1/users/{owner_id}",
            missing=f"Owner {owner_id} does not exist",
            unreachable_hint="cannot verify restaurant owner",
            bad_gateway_hint="verifying owner",
        )
        if owner.get("role") != OWNER_ROLE:
            raise forbidden(
                f"Owner {owner_id} has role '{owner.get('role')}' and is not authorised "
                f"to onboard restaurants (requires '{OWNER_ROLE}')"
            )
        return owner
