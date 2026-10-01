"""The one shape `POST /orders/{id}/internal/events` accepts (D53).

Deliberately generic rather than one schema per event type: `event_type` is what selects the
schema `common.events.order.EVENT_DATA_MODELS` validates `data` against, downstream of this
model — duplicating that dispatch into five near-identical Pydantic classes here would be a
second copy of the same map.
"""

from typing import Any

from pydantic import BaseModel


class PublishEventRequest(BaseModel):
    event_type: str
    data: dict[str, Any]
