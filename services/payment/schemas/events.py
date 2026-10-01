"""The one shape `POST /api/v1/payments/internal/events` accepts (D53) — see
order/schemas/events.py for the identical reasoning on the order side."""

from typing import Any

from pydantic import BaseModel


class PublishEventRequest(BaseModel):
    event_type: str
    data: dict[str, Any]
