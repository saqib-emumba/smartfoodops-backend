"""Outbound calls to sibling services.

Restaurant existence and active state live in PostgreSQL, which this service must not
read, so they are resolved over HTTP against the Restaurant Service.
"""

import os
from logging import Logger
from uuid import UUID

from common.auth import bearer
from common.config import DEFAULT_RESTAURANT_SERVICE_URL
from common.errors import not_found
from common.service_client import ServiceClient

RESTAURANT_SERVICE_URL = os.getenv(
    "RESTAURANT_SERVICE_URL", DEFAULT_RESTAURANT_SERVICE_URL
)


class RestaurantServiceClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient(
            "Restaurant Service", RESTAURANT_SERVICE_URL, logger=logger
        )

    @property
    def base_url(self) -> str:
        return self._client.base_url

    async def verify_active(self, restaurant_id: UUID, token: str) -> dict:
        """Confirm the restaurant exists and is active; an inactive one reads as absent.

        Returns the record so the caller can check who owns it — see main.upsert_menu.
        """
        restaurant = await self._client.aget(
            f"/api/v1/restaurants/{restaurant_id}",
            missing=f"Restaurant {restaurant_id} does not exist",
            unreachable_hint="cannot verify restaurant",
            headers=bearer(token),
        )
        if not restaurant.get("is_active", False):
            raise not_found(f"Restaurant {restaurant_id} is not active")
        return restaurant
