"""Pydantic v2 validation schemas for the SmartFoodOps Order Service."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrderItemSelection(BaseModel):
    item_id: str
    quantity: int = Field(..., gt=0)
    customizations: Optional[dict] = None  # To capture dynamic modifications


class OrderItemSnapshot(OrderItemSelection):
    """Server-side priced line item persisted into the orders.items JSONB column."""

    name: Optional[str] = None
    unit_price: Optional[float] = None
    line_total: Optional[float] = None
    selected_options: List[dict] = []


class OrderCreateRequest(BaseModel):
    # No customer_id: the customer is whoever the access token says it is. Accepting it
    # from the body would let any caller place an order in someone else's name.
    restaurant_id: UUID
    items: List[OrderItemSelection] = Field(..., min_length=1)
    total_amount: float = Field(..., gt=0.0)
    idempotency_key: Optional[str] = Field(
        None, description="Client-provided unique transaction tracking ID"
    )


class OrderResponse(BaseModel):
    id: UUID
    customer_id: UUID
    restaurant_id: UUID
    # Written by the saga when a rider is assigned. The column has existed since Week 1 and
    # nothing ever set it; the dispatch step is the first thing that does. Additive, so the
    # Payment Service and the smoke test read the same shape they always did.
    rider_id: Optional[UUID] = None
    items: List[OrderItemSnapshot]
    total_amount: float
    status: str
    idempotency_key: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class OrderTrackingLogCreateRequest(BaseModel):
    """One reported state transition, on the wire.

    Unchanged from the shape the Menu Service accepted while the trail lived in MongoDB,
    so the move to `order_tracking_logs` cost callers nothing. `status` is validated
    against the `order_status` enum by the database rather than restated here — there is
    one list of valid statuses and it is the one the `orders` table already uses.
    """

    order_id: UUID
    status: str
    service: str
    raw_log: str
    updated_by: Optional[str] = "system"
    metadata: Optional[dict] = None


class WorkflowSignalRequest(BaseModel):
    """One event reported by a sibling service, on its way into the order's workflow.

    `signal` is constrained to the three the workflow actually handles, so a typo is a 422
    here rather than a signal Temporal accepts and nothing ever reads — an unhandled signal
    name is silently dropped by the SDK, which would be an event that vanishes.
    """

    signal: Literal["restaurant_decision", "rider_pickup", "rider_delivery"]
    payload: dict = {}


class OrderTrackingLogResponse(BaseModel):
    """A persisted entry. `previous_status` is derived server-side from the entry before
    it, so a caller cannot report a transition that contradicts the recorded history."""

    id: UUID
    order_id: UUID
    previous_status: Optional[str] = None
    status: str
    service: str
    updated_by: str
    raw_log: Optional[str] = None
    metadata: dict = {}
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
