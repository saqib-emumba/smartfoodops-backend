"""Pydantic v2 validation schemas for the SmartFoodOps Restaurant Service."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RestaurantOnboardRequest(BaseModel):
    owner_id: UUID
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
