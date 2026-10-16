"""Request and response shapes for the restaurant-admin authoring tools (Week 5, D64)."""

from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _PartnerRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # Every tool is scoped to a restaurant the caller owns: the handler verifies it against the
    # Restaurant Service before anything is generated.
    restaurant_id: UUID


class DescribeDishRequest(_PartnerRequest):
    dish_name: str = Field(..., min_length=2, max_length=120)
    ingredients: List[str] = Field(..., min_length=1, max_length=20)
    tone: Literal["casual", "upscale", "playful"] = "casual"


class DescribeDishResponse(BaseModel):
    dish_name: str
    description: str
    grounded: bool


class PromoRequest(_PartnerRequest):
    offer: str = Field(..., min_length=5, max_length=300)
    channel: Literal["social", "sms", "email"] = "social"


class PromoResponse(BaseModel):
    promo_text: str
    grounded: bool


class ReviewSummaryRequest(_PartnerRequest):
    pass


class ReviewSummaryResponse(BaseModel):
    restaurant_id: UUID
    # From the database, never from the model: the model only writes the prose.
    review_count: int
    average_rating: Optional[float] = None
    reviews_analysed: int
    summary: Optional[str] = None
