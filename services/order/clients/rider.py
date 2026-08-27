"""The saga's fleet calls: dispatch and release, on the internal key.

Used only by activities.py — see clients/__init__.py for why the internal key rather than
a forwarded bearer token.
"""

from uuid import UUID

from common.auth import internal_headers
from common.config import DEFAULT_RIDER_SERVICE_URL
from common.errors import unprocessable
from common.service_client import ServiceFacade


class SagaRiderClient(ServiceFacade):
    display_name = "Rider Service"
    env_var = "RIDER_SERVICE_URL"
    default_url = DEFAULT_RIDER_SERVICE_URL

    def dispatch(self, order_id: UUID, latitude: float, longitude: float) -> dict:
        return self._client.post(
            "/api/v1/riders/dispatch",
            json={
                "order_id": str(order_id),
                "restaurant_latitude": latitude,
                "restaurant_longitude": longitude,
            },
            missing=f"Order {order_id} cannot be dispatched",
            missing_error=unprocessable,
            unreachable_hint="cannot dispatch a rider",
            bad_gateway_hint="dispatching a rider",
            headers=internal_headers(),
        )

    def release(self, order_id: UUID) -> dict:
        return self._client.post(
            "/api/v1/riders/release",
            json={"order_id": str(order_id)},
            missing=f"Order {order_id} has no rider to release",
            missing_error=unprocessable,
            unreachable_hint="cannot release the rider",
            bad_gateway_hint="releasing the rider",
            headers=internal_headers(),
        )
