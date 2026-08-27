"""The kitchen's narrower view of an order, and its decision outcome."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from order.schemas.orders import OrderItemSnapshot


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
