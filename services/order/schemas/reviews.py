"""Customer reviews of delivered orders (Week 5, D64)."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrderReviewRequest(BaseModel):
    # Whitespace is stripped before the length check, so a comment of spaces is "no comment".
    model_config = ConfigDict(str_strip_whitespace=True)

    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = Field(None, max_length=1000)


class OrderReviewResponse(BaseModel):
    id: UUID
    order_id: UUID
    restaurant_id: UUID
    rating: int
    comment: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReviewEntry(BaseModel):
    """One review as the AI Service reads it. No customer id: the consumer is a language model's
    prompt, and who wrote a review has no business there."""

    rating: int
    comment: Optional[str] = None
    created_at: datetime


class RestaurantReviews(BaseModel):
    restaurant_id: UUID
    review_count: int
    average_rating: Optional[float] = None
    # Newest first, capped by the caller's `limit`. Only reviews that carry a comment are listed;
    # `review_count` and `average_rating` still cover all of them.
    reviews: List[ReviewEntry]
