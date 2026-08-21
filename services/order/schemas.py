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
    # The kitchen's answer. NULL until they respond; `status` stays `confirmed` either way,
    # because acceptance does not move the order along its lifecycle — finding a rider does.
    kitchen_decision: Optional[str] = None
    idempotency_key: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class KitchenOrderResponse(BaseModel):
    """One order as the *kitchen* sees it, which is deliberately less than OrderResponse.

    An admin deciding whether to cook something needs to know what was ordered. They have
    no business seeing what the customer paid, which idempotency key the client chose, or
    who the customer is — so none of those fields are here. This projection is the reason
    moving the kitchen queue onto `orders` did not also widen what a restaurant can read.
    """

    id: UUID
    restaurant_id: UUID
    items: List[OrderItemSnapshot]
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class KitchenDecisionResponse(BaseModel):
    """The outcome of an accept or reject.

    `changed` is False when the order had already been decided, or had already been
    cancelled out from under the kitchen by a saga timeout — in which case `decision`
    reports whatever actually stuck rather than what was asked for.
    """

    order_id: UUID
    decision: Optional[str] = None
    status: str
    changed: bool


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

    `signal` is constrained to the ones the workflow actually handles, so a typo is a 422
    here rather than a signal Temporal accepts and nothing ever reads — an unhandled signal
    name is silently dropped by the SDK, which would be an event that vanishes.

    `restaurant_decision` is deliberately absent. Since D32 a kitchen decision arrives
    through `POST /api/v1/orders/{id}/accept|reject`, which is authenticated as the owning
    admin and writes the decision before signalling. Leaving it reachable here as well would
    be a second, unauthenticated way to do the same thing — exactly the drift D16 warns
    about. This relay now carries rider events only.
    """

    signal: Literal["rider_pickup", "rider_delivery"]
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
