"""Pydantic v2 validation schemas for the SmartFoodOps Menu Service."""

from datetime import datetime
from typing import List
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from common.dietary import normalize_dietary_tags


class CustomOption(BaseModel):
    name: str
    extra_price: float = Field(0.0, ge=0.0)


class CustomizationGroup(BaseModel):
    group_id: str
    group_name: str
    min_selection: int = Field(1, ge=0)
    max_selection: int = Field(1, ge=1)
    options: List[CustomOption]

    @model_validator(mode="after")
    def check_selection_bounds(self) -> "CustomizationGroup":
        """A group whose minimum exceeds its maximum can never be satisfied -> 422."""
        if self.min_selection > self.max_selection:
            raise ValueError(
                f"customization group '{self.group_id}': min_selection "
                f"({self.min_selection}) cannot be greater than max_selection ({self.max_selection})"
            )
        return self


class MenuItem(BaseModel):
    item_id: str
    name: str
    description: str
    base_price: float = Field(..., gt=0.0)
    is_available: bool = True
    # Owner-declared, from `common.dietary.DIETARY_TAGS` (D60). Optional: an item with no tags
    # simply never matches a dietary filter, rather than being assumed to match any of them.
    dietary_tags: List[str] = []
    customization_groups: List[CustomizationGroup] = []

    @field_validator("dietary_tags")
    @classmethod
    def check_dietary_tags(cls, value: List[str]) -> List[str]:
        return normalize_dietary_tags(value)


class MenuCategory(BaseModel):
    category_id: str
    category_name: str
    display_order: int = Field(1, ge=1)
    items: List[MenuItem]


class MenuUpsertRequest(BaseModel):
    restaurant_id: UUID
    categories: List[MenuCategory]


class MenuResponse(BaseModel):
    restaurant_id: UUID
    categories: List[MenuCategory]


class InternalMenuResponse(MenuResponse):
    """What `GET /menus/{id}/internal` returns: the menu plus `updated_at`, the version stamp
    the AI Service ingests it under (`menus.updated_at` is the one column a publish reliably
    changes — every category and item row is re-created on each publish, so theirs mean
    nothing)."""

    updated_at: datetime


class PublishedMenuRef(BaseModel):
    """One restaurant that has a published menu, for a full re-ingest to walk."""

    restaurant_id: UUID
    updated_at: datetime
