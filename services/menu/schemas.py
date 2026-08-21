"""Pydantic v2 validation schemas for the SmartFoodOps Menu Service."""

from typing import List
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


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
    dietary_flags: List[str] = []
    customization_groups: List[CustomizationGroup] = []


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
