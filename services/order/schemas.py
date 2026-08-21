"""Pydantic v2 validation schemas for the SmartFoodOps Order Service."""

from datetime import datetime
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
