"""Request and response shapes for `POST /api/v1/ai/rag-context` (Week 4, D62)."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.dietary import normalize_dietary_tags
from ai.schemas.search import ItemMatch, RestaurantMatch


class RagContextRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    customer_id: UUID
    prompt_query: str = Field(..., min_length=2, max_length=500)
    # Extends the spec's body (D62): `customer_id` alone carries no location, so "near me" would
    # have nothing to measure from. Both or neither.
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    top_k: int = Field(5, ge=1, le=20)
    max_price: Optional[float] = Field(None, gt=0)
    dietary_filters: List[str] = []

    @field_validator("dietary_filters")
    @classmethod
    def check_filters(cls, value: List[str]) -> List[str]:
        return normalize_dietary_tags(value)

    @model_validator(mode="after")
    def check_location_pair(self) -> "RagContextRequest":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be given together")
        return self


class RagRestaurant(RestaurantMatch):
    # Kilometres from the caller's location; None when no location was supplied.
    distance_km: Optional[float] = None


class FavouriteVendor(BaseModel):
    restaurant_id: UUID
    # Resolved from the vector store's restaurant documents, because the language model needs a
    # name, not an id. None if that restaurant is not (or no longer) indexed.
    name: Optional[str] = None
    is_active: Optional[bool] = None
    delivered_orders: int
    last_ordered_at: Optional[datetime] = None


class CustomerProfile(BaseModel):
    """What this customer has ordered before, from the analytics projection.

    Restaurants only, not dishes: the projection holds no line items, so nothing here can claim
    a favourite dish. All zeros and an empty list means "no history", which is itself a usable
    answer for the assistant ("nothing to personalise from").
    """

    total_orders: int
    delivered_orders: int
    cancelled_orders: int
    last_order_at: Optional[datetime] = None
    favourite_vendors: List[FavouriteVendor]


class CourierAvailability(BaseModel):
    """How many riders could take an order from one restaurant right now."""

    restaurant_id: UUID
    available_riders: int
    nearest_available_km: Optional[float] = None
    radius_km: float


class RagContextResponse(BaseModel):
    query: str
    items: List[ItemMatch]
    restaurants: List[RagRestaurant]
    customer_profile: Optional[CustomerProfile] = None
    courier_availability: List[CourierAvailability]
    # Sources that could not be read: "vector", "analytics" or "courier". A source listed here
    # contributed nothing, and the consumer must say so rather than treat its absence as "none".
    sources_failed: List[str]
