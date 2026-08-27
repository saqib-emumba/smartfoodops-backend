"""The published menu an order is priced against, fetched from the Menu Service."""

from uuid import UUID

from common.auth import bearer
from common.config import DEFAULT_MENU_SERVICE_URL
from common.service_client import ServiceFacade


class MenuServiceClient(ServiceFacade):
    display_name = "Menu Service"
    env_var = "MENU_SERVICE_URL"
    default_url = DEFAULT_MENU_SERVICE_URL

    def fetch_menu(self, restaurant_id: UUID, token: str) -> dict:
        """Pull the restaurant's published menu, the source of truth for pricing."""
        return self._client.get(
            f"/api/v1/menus/{restaurant_id}",
            missing=f"No active menu found for restaurant {restaurant_id}",
            unreachable_hint="cannot validate the order",
            headers=bearer(token),
        )
