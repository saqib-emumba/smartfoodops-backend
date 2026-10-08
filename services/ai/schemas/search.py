"""Request and response shapes for `POST /api/v1/ai/search` (Week 4, D59)."""

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.dietary import normalize_dietary_tags


class SearchRequest(BaseModel):
    # Whitespace is stripped *before* `min_length` is checked, so "   " is rejected rather than
    # embedded: an empty query has no meaning to retrieve against.
    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(..., min_length=2, max_length=500)
    top_k: int = Field(5, ge=1, le=20)
    # Hard SQL constraints, applied beside the vector ordering — not hints to it.
    max_price: Optional[float] = Field(None, gt=0)
    # Every tag listed must be present on a match (array containment). Validated against the
    # shared vocabulary (common/dietary.py) so a typo is a 422, not a silently empty result.
    dietary_filters: List[str] = []
    restaurant_id: Optional[UUID] = None

    @field_validator("dietary_filters")
    @classmethod
    def check_filters(cls, value: List[str]) -> List[str]:
        return normalize_dietary_tags(value)


class ItemMatch(BaseModel):
    # `item_id` is the menu's own key — the same value the order API takes as `item_id`.
    item_id: str
    item_name: str
    restaurant_id: UUID
    restaurant_name: str
    category_name: str
    price: float
    dietary_tags: List[str]
    similarity_score: float


class RestaurantMatch(BaseModel):
    restaurant_id: UUID
    name: str
    address: Optional[str] = None
    similarity_score: float


class SearchResponse(BaseModel):
    matches: List[ItemMatch]
    restaurants: List[RestaurantMatch]
