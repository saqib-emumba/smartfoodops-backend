"""Recording a delivery event against the order it belongs to."""

from uuid import UUID

from common.auth import internal_headers
from common.config import DEFAULT_ORDER_SERVICE_URL
from common.service_client import ServiceFacade


class OrderServiceClient(ServiceFacade):
    display_name = "Order Service"
    env_var = "ORDER_SERVICE_URL"
    default_url = DEFAULT_ORDER_SERVICE_URL

    async def record_rider_report(self, order_id: UUID, stage: str) -> dict:
        """Record that this rider reached `stage` on this order, before signalling the saga.

        Until D47 this method was `signal(...)`, posting to `/orders/{id}/signals` and
        letting the Order Service relay the event into the workflow on this service's
        behalf. This service now holds its own Temporal client and sends the signal itself —
        so what is left here is the *write*, not the relay.

        The call survives the change because an order's state is the Order Service's to hold
        (D01), and `orders.rider_reported_stage` is what the saga's
        `read_rider_report_activity` reads when the delivery timer expires. Writing it into
        `sfo_rider_core` instead would move order state into this service and repoint that
        activity for no gain.

        Ordering is the caller's responsibility and it is load-bearing: this must be awaited
        *before* the signal goes out. See `fleet.report_event`.

        `apost`, not `post`, because the two reporting routes became `async def` when they
        started awaiting the signal — and a blocking `post()` on the event loop would stall
        every other request in this process for the duration. That is the exact reason
        `common/service_client.py` grew `apost` in the first place.
        """
        return await self._client.apost(
            f"/api/v1/orders/{order_id}/rider-report",
            json={"stage": stage},
            missing=f"Order {order_id} does not exist",
            unreachable_hint="cannot record the delivery update",
            bad_gateway_hint="recording the delivery update",
            headers=internal_headers(),
        )
