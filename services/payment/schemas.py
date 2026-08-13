"""Pydantic v2 validation schemas for the SmartFoodOps Payment Service."""

from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PaymentCreateRequest(BaseModel):
    order_id: UUID = Field(..., description="Logical reference to an order id")
    amount: float = Field(..., gt=0.0, description="Amount to authorise")
    idempotency_key: str = Field(
        ...,
        min_length=1,
        description="Mandatory unique transaction key; must equal the X-Idempotency-Key header",
    )


class PaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    amount: float
    status: str
    transaction_reference: Optional[str] = None
    idempotency_key: str

    model_config = ConfigDict(from_attributes=True)
