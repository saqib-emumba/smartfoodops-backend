"""What the Order Service sends to start the order saga or relay a signal into one — the
API-side half of the HTTP boundary D36 drew between it and this service.

Named for the entity — `OrderSaga*`, not `Saga*` — so a future second entity's request and
response shapes, which will not share these fields, sit in `schemas/<entity>.py` under
their own names without colliding with these.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class OrderSagaStartRequest(BaseModel):
    """Everything `OrderWorkflow.run` takes as its argument, captured once at checkout.

    Every field here was already being read by `order/apis/checkout.py` before the saga
    existed to hand it onward — see `order/clients/orchestrator.py::start_saga`, this
    request's only caller.
    """

    order_id: UUID
    restaurant_id: UUID
    # A string, not a float: an exact decimal has to survive the JSON boundary into
    # workflow history, and D07's guarantee stops at that boundary.
    amount: str
    # Snapshotted at checkout, so the saga never has to call the Restaurant Service (D32).
    capacity: int
    restaurant_latitude: float
    restaurant_longitude: float


class OrderSagaStartResponse(BaseModel):
    workflow_id: str
    order_id: UUID


class OrderSagaSignalRequest(BaseModel):
    """One event to relay into a running saga. `restaurant_decision` reaches this from
    kitchen.py; `rider_pickup`/`rider_delivery` arrive relayed through the Order Service's
    own signal endpoint, which a sibling calls without knowing an orchestrator exists."""

    signal: Literal["restaurant_decision", "rider_pickup", "rider_delivery"]
    payload: dict = {}


class OrderSagaSignalResponse(BaseModel):
    signalled: str
    order_id: UUID
