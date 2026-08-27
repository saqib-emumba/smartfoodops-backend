"""Pydantic v2 validation schemas for the SmartFoodOps Restaurant Service."""

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
