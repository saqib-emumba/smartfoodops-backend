"""The Rider Service, as the RAG assembler reads it (internal route, D62)."""

from common.auth import internal_headers
from common.config import DEFAULT_RIDER_SERVICE_URL
from common.service_client import ServiceFacade


class RiderServiceClient(ServiceFacade):
    display_name = "Rider Service"
    env_var = "RIDER_SERVICE_URL"
    default_url = DEFAULT_RIDER_SERVICE_URL

    def nearby(self, latitude: float, longitude: float) -> dict:
        """`{available_riders, nearest_available_km, radius_km}` around a point.

        Aggregates only — the Rider Service never returns a rider id or position here, because
        the answer ends up in an LLM prompt.
        """
        return self._client.post(
            "/api/v1/riders/internal/nearby",
            json={"latitude": latitude, "longitude": longitude},
            missing="Courier availability endpoint not found",
            unreachable_hint="cannot read courier availability",
            headers=internal_headers(),
        )
