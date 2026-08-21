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


class PaymentAuthorizeRequest(BaseModel):
    """Sent by the order saga's payment activity, not by any end user.

    `amount` is a **string** here where `PaymentCreateRequest` takes a float, and that is
    deliberate. A workflow argument is a JSON boundary, and D07 keeps money exact right up
    to one: `"27.00"` survives the round trip, while `27.0` is a binary float that may not
    equal the total this payment has to settle to the cent.
    """

    order_id: UUID
    amount: str = Field(..., min_length=1, description="Exact decimal amount, as a string")
    idempotency_key: str = Field(
        ...,
        min_length=1,
        description="Derived from the order id by the workflow, so retries collapse",
    )


class PaymentRefundRequest(BaseModel):
    """The saga's compensating action."""

    order_id: UUID
    reason: Optional[str] = Field(None, description="Why the saga could not complete")


class PaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    amount: float
    status: str
    transaction_reference: Optional[str] = None
    idempotency_key: str

    model_config = ConfigDict(from_attributes=True)
