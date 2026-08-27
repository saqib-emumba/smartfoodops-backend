"""The order saga's one call back into the Order Service: recording a transition and
reading the kitchen's decision — both, before D36, direct writes and reads against
`sfo_order_core` through a shared `OrderRepository`, back when the worker ran inside the
Order Service's own image and database. It no longer does either.

Named `order_service.py`, not `order.py`, precisely because it sits inside
`clients/order/` — the entity subpackage — and calling it `order.py` there would read as
"the order entity's client for the order entity", which is not what it is: it is this
entity's client *for the Order Service specifically*, alongside its siblings `payment.py`
and `rider.py` in the same directory.

`read` reuses the internal read endpoint the request path already exposes
(`GET /orders/{id}/internal`) — nothing new was needed there, since `OrderResponse` already
carries `kitchen_decision`. `transition` is the one route this split actually had to add;
see order/apis/transitions.py.
"""

from uuid import UUID

from common.auth import internal_headers
from common.config import DEFAULT_ORDER_SERVICE_URL
from common.service_client import ServiceFacade

# Who this worker stamps into order_tracking_logs.service, replacing the fixed
# `service_name="order-worker"` a locally-held OrderRepository used to carry.
SERVICE_NAME = "orchestrator-worker"


class AtCapacity(Exception):
    """The kitchen's rail is full — the transition endpoint answered `409`.

    Mirrors the role `order.repositories.orders.AtCapacity` played when this was a Python
    exception raised in-process; raised here from the HTTP boundary instead, via
    `common.service_client`'s `passthrough` mechanism, so activities.py can convert it to a
    non-retryable `ApplicationError` without importing anything FastAPI-shaped.
    """


class OrderGone(Exception):
    """The order no longer exists — the endpoint answered `404`."""


class OrderServiceClient(ServiceFacade):
    display_name = "Order Service"
    env_var = "ORDER_SERVICE_URL"
    default_url = DEFAULT_ORDER_SERVICE_URL

    def transition(
        self,
        order_id: UUID | str,
        *,
        status: str,
        updated_by: str = "order-workflow",
        event: dict | None = None,
        metadata: dict | None = None,
        rider_id: UUID | str | None = None,
        capacity_limit: int | None = None,
    ) -> dict:
        """Record a status transition. Returns `{"status": ..., "changed": ...}` — the
        same shape `OrderRepository.transition` always returned to the activity that calls
        this, so `transition_order_activity` did not have to change its own contract."""
        return self._client.post(
            f"/api/v1/orders/{order_id}/transitions",
            json={
                "status": status,
                "updated_by": updated_by,
                "service": SERVICE_NAME,
                "event": event,
                "metadata": metadata,
                "rider_id": str(rider_id) if rider_id else None,
                "capacity_limit": capacity_limit,
            },
            missing=f"Order {order_id} no longer exists",
            missing_error=OrderGone,
            unreachable_hint="cannot record the order's transition",
            headers=internal_headers(),
            passthrough={409: AtCapacity},
        )

    def read(self, order_id: UUID | str) -> dict:
        """The order as `read_kitchen_decision_activity` needs it — `kitchen_decision` and
        `status` — reusing the same internal endpoint the checkout flow's saga hand-off
        already reads."""
        return self._client.get(
            f"/api/v1/orders/{order_id}/internal",
            missing=f"Order {order_id} no longer exists",
            missing_error=OrderGone,
            unreachable_hint="cannot read the order",
            headers=internal_headers(),
        )
