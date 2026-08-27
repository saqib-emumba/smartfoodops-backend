"""Line items and the customer-facing checkout shape."""

from typing import List, Optional
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
    # The kitchen's answer. NULL until they respond; `status` stays `confirmed` either way,
    # because acceptance does not move the order along its lifecycle — finding a rider does.
    kitchen_decision: Optional[str] = None
    idempotency_key: Optional[str]

    model_config = ConfigDict(from_attributes=True)
