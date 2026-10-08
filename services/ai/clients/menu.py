"""The Menu Service, as the ingestion worker reads it (internal routes, D61).

Internal-key only: the worker is not a user and holds no token, so it asserts itself with the
shared key (D15) the same way the orchestrator's activities do.
"""

from common.auth import internal_headers
from common.config import DEFAULT_MENU_SERVICE_URL
from common.service_client import ServiceFacade


class MenuServiceClient(ServiceFacade):
    display_name = "Menu Service"
    env_var = "MENU_SERVICE_URL"
    default_url = DEFAULT_MENU_SERVICE_URL

    def get_menu(self, restaurant_id: str) -> dict:
        """The menu tree plus `updated_at`. A 404 (`HTTPException`) means no menu exists."""
        return self._client.get(
            f"/api/v1/menus/{restaurant_id}/internal",
            missing=f"No menu published for restaurant {restaurant_id}",
            unreachable_hint="cannot read the menu",
            headers=internal_headers(),
        )

    def list_published(self) -> list[dict]:
        """`[{restaurant_id, updated_at}]` for every restaurant with a menu."""
        body = self._client.get(
            "/api/v1/menus/internal/restaurant-ids",
            missing="Menu listing endpoint not found",
            unreachable_hint="cannot list published menus",
            headers=internal_headers(),
        )
        # `ServiceClient` normalises an empty body to `{}`, and an empty *list* is falsy too.
        return body if isinstance(body, list) else []
