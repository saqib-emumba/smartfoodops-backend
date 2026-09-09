"""`data` shapes for every `order.*` event type, and the map from event type to model.

One model per *payload shape*, not per event type — `order.confirmed`, `order.assigned`,
`order.picked_up`, `order.delivered` and `order.cancelled` all come from
`OrderRepository.transition()` and carry exactly the same fields (see
`order/repositories/orders.py`), so `OrderTransitionData` covers all five. Each still gets
its own registered schema subject, keyed by event type — see `EVENT_DATA_MODELS` — because
a producer and a consumer agree on "what does an `order.cancelled` event look like", and the
fact that it happens to share a Python class with `order.assigned` is an implementation
detail on this side only.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class OrderCreatedData(BaseModel):
    order_id: UUID
    customer_id: UUID
    restaurant_id: UUID
    total_amount: Decimal
    status: str
    items_count: int


class OrderTransitionData(BaseModel):
    """`order.confirmed` / `order.assigned` / `order.picked_up` / `order.delivered` /
    `order.cancelled`. `metadata` carries whatever `OrderRepository.transition()` was
    called with — for `order.cancelled` specifically, the saga's compensation `reason` and
    `detail`, which is how a cause recorded nowhere the orchestrator can write reaches
    Kafka at all (D38: the orchestrator holds no outbox of its own).
    """

    order_id: UUID
    new_status: str
    customer_id: UUID
    restaurant_id: UUID
    rider_id: UUID | None = None
    total_amount: Decimal
    metadata: dict[str, Any] = {}


class OrderKitchenDecidedData(BaseModel):
    order_id: UUID
    decision: str
    restaurant_id: UUID


# event_type -> (pydantic model, event_version). The version here is what gets registered
# with the Schema Registry (Week 3, D40) and what a producer stamps on the envelope — bump
# it, and register a new model, on any breaking change to a shape below.
EVENT_DATA_MODELS: dict[str, tuple[type[BaseModel], int]] = {
    "order.created": (OrderCreatedData, 1),
    "order.confirmed": (OrderTransitionData, 1),
    "order.assigned": (OrderTransitionData, 1),
    "order.picked_up": (OrderTransitionData, 1),
    "order.delivered": (OrderTransitionData, 1),
    "order.cancelled": (OrderTransitionData, 1),
    "order.kitchen.decided": (OrderKitchenDecidedData, 1),
}
