"""Reporting a delivery event into the order's workflow, via the Order Service."""

from uuid import UUID

from common.auth import internal_headers
from common.config import DEFAULT_ORDER_SERVICE_URL
from common.service_client import ServiceFacade


class OrderServiceClient(ServiceFacade):
    display_name = "Order Service"
    env_var = "ORDER_SERVICE_URL"
    default_url = DEFAULT_ORDER_SERVICE_URL

    def signal(self, order_id: UUID, signal: str, payload: dict) -> dict:
        """Report a delivery event into the order's workflow.

        This service does not know Temporal exists. It reports what it observed to the
        service that owns the order lifecycle, exactly as it would report any other status
        transition, and the Order Service is what relays it into the saga.
        """
        return self._client.post(
            f"/api/v1/orders/{order_id}/signals",
            json={"signal": signal, "payload": payload},
            missing=f"Order {order_id} has no running workflow to notify",
            unreachable_hint="cannot report the delivery update",
            bad_gateway_hint="reporting the delivery update",
            headers=internal_headers(),
        )
