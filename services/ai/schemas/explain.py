"""Request and response shapes for `POST /api/v1/ai/explain-order` (Week 5, D64)."""

from uuid import UUID

from pydantic import BaseModel


class ExplainOrderRequest(BaseModel):
    order_id: UUID


class ExplainOrderResponse(BaseModel):
    order_id: UUID
    explanation: str
    # False when the model's wording was replaced by one written from the facts alone because it
    # quoted a figure the facts did not contain (see ai/guardrails.py).
    grounded: bool
