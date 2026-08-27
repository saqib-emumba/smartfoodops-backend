"""Pydantic v2 validation schemas for the SmartFoodOps Rider Service."""

from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RiderRegisterRequest(BaseModel):
    # No user_id: the rider is whoever the access token says it is. Accepting it from the
    # body would let any caller enrol somebody else into the fleet (D13).
    vehicle_type: str = Field(..., min_length=2, max_length=100)
    vehicle_number: str = Field(..., min_length=2, max_length=100)
    # A rider who has never reported a location is not dispatchable, so both are optional
    # here and simply exclude the rider from the partial index until they check in.
    current_latitude: Optional[float] = Field(None, ge=-90, le=90)
    current_longitude: Optional[float] = Field(None, ge=-180, le=180)


class RiderLocationRequest(BaseModel):
    """A location ping. Both coordinates are required together — a latitude without a
    longitude is not a position, and dispatch treats a half-set pair as no position."""

    current_latitude: float = Field(..., ge=-90, le=90)
    current_longitude: float = Field(..., ge=-180, le=180)


class RiderAvailabilityRequest(BaseModel):
    """A rider going on or off shift. Cannot be used to abandon an order: the handler
    refuses while `current_order_id` is set."""

    is_available: bool


class RiderResponse(BaseModel):
    id: UUID
    user_id: UUID
    vehicle_type: str
    vehicle_number: str
    is_available: bool
    current_latitude: Optional[float] = None
    current_longitude: Optional[float] = None
    current_order_id: Optional[UUID] = None

    model_config = ConfigDict(from_attributes=True)


class DispatchRequest(BaseModel):
    """Sent by the saga's dispatch activity, not by any end user.

    The coordinates are the restaurant's, and they arrive here rather than being looked up
    because the activity already holds them — it is the caller that knows which restaurant
    the order belongs to.
    """

    order_id: UUID
    restaurant_latitude: float = Field(..., ge=-90, le=90)
    restaurant_longitude: float = Field(..., ge=-180, le=180)
    max_distance_km: Optional[float] = Field(None, gt=0)


class DispatchResponse(BaseModel):
    """The outcome of one dispatch attempt.

    `assigned: false` is a `200`, not an error. An empty fleet is an answer the workflow
    acts on by waiting and asking again; a `503` would mean the Rider Service is broken,
    and conflating the two would make the saga retry the wrong thing.
    """

    assigned: bool
    order_id: UUID
    rider_id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    distance_km: Optional[float] = None
    eta_minutes: Optional[int] = None
    reason: Optional[str] = None


class ReleaseRequest(BaseModel):
    order_id: UUID


class ReleaseResponse(BaseModel):
    """`released: false` means nothing held this order, which is success for a compensating
    action — the fleet is already in the state the caller wanted."""

    released: bool
    order_id: UUID
    rider_id: Optional[UUID] = None
