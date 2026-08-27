"""Restaurant existence and active state, verified against the Restaurant Service.

This service must not read PostgreSQL owned by another service, so both facts are resolved
over HTTP instead of by a join.
"""

from uuid import UUID

from common.auth import bearer
from common.config import DEFAULT_RESTAURANT_SERVICE_URL
from common.errors import not_found
from common.service_client import ServiceFacade


class RestaurantServiceClient(ServiceFacade):
    display_name = "Restaurant Service"
    env_var = "RESTAURANT_SERVICE_URL"
    default_url = DEFAULT_RESTAURANT_SERVICE_URL

    async def verify_active(self, restaurant_id: UUID, token: str) -> dict:
        """Confirm the restaurant exists and is active; an inactive one reads as absent.

        Returns the record so the caller can check who owns it — see api.upsert_menu.
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
