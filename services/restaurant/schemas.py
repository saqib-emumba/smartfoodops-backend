"""Pydantic v2 validation schemas for the SmartFoodOps Restaurant Service."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RestaurantOnboardRequest(BaseModel):
    # No owner_id: the owner is the subject of the access token. Taking it from the body
    # would let anyone onboard a restaurant under another account's name.
    name: str = Field(..., min_length=2)
    address: str = Field(..., min_length=5)
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    capacity: int = Field(50, gt=0)


class RestaurantResponse(BaseModel):
    id: UUID
    owner_id: UUID
    name: str
    address: str
    latitude: float
    longitude: float
    is_active: bool
    capacity: int

    model_config = ConfigDict(from_attributes=True)


class TicketCreateRequest(BaseModel):
    """Sent by the order saga's ticket activity, not by any end user."""

    order_id: UUID
    restaurant_id: UUID
    # The lines as the Order Service priced them, so the kitchen sees what was bought
    # without calling back. A snapshot, not a live reference.
    items: List[dict] = []


class TicketResponse(BaseModel):
    id: UUID
    order_id: UUID
    restaurant_id: UUID
    items: List[dict] = []
    status: str
    decided_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TicketCreateResponse(BaseModel):
    """The outcome of presenting one order to one kitchen.

    `queued: false` with `reason: "at_capacity"` is a `200`, not an error. A full kitchen
    is a business answer the saga compensates for; a `503` would mean the Restaurant
    Service is broken, and the workflow retries those two very differently.
    """

    queued: bool
    order_id: UUID
    reason: Optional[str] = None
    ticket: Optional[TicketResponse] = None
