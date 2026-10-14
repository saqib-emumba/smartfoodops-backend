"""The Restaurant Service, as the ingestion worker reads it (internal route, D61)."""

from common.auth import internal_headers
from common.config import DEFAULT_RESTAURANT_SERVICE_URL
from common.service_client import ServiceFacade


class RestaurantServiceClient(ServiceFacade):
    display_name = "Restaurant Service"
    env_var = "RESTAURANT_SERVICE_URL"
    default_url = DEFAULT_RESTAURANT_SERVICE_URL

    def get_restaurant(self, restaurant_id: str) -> dict:
        return self._client.get(
            f"/api/v1/restaurants/{restaurant_id}/internal",
            missing=f"Restaurant {restaurant_id} not found",
            unreachable_hint="cannot read the restaurant",
            headers=internal_headers(),
        )
