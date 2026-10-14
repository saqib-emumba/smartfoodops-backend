"""Response shapes for the Analytics Service's internal customer read (Week 4, D62)."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class FavouriteRestaurant(BaseModel):
    restaurant_id: UUID
    delivered_orders: int
    last_ordered_at: Optional[datetime] = None


class CustomerSummary(BaseModel):
    """A customer's order history, derived from `order_projections`.

    Restaurants only, not dishes: the projection holds `restaurant_id` per order but no line
    items (`order.created` carries none), so "your favourite dish" is not derivable here and is
    deliberately not claimed.
    """

    customer_id: UUID
    total_orders: int
    delivered_orders: int
    cancelled_orders: int
    last_order_at: Optional[datetime] = None
    favourite_restaurants: list[FavouriteRestaurant]
