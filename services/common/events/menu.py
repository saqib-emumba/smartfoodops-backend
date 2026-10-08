"""`data` shape for the `menu.published` event (Week 4, D61).

Deliberately thin: it says *that* a restaurant's menu changed, not *what* it now contains.
The consumer (the AI Service's ingestion worker) re-reads the menu over the Menu Service's
internal route, so the event never has to carry — or version — the whole tree, and a consumer
that was down for ten publishes simply reads the latest state once.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class MenuPublishedData(BaseModel):
    restaurant_id: UUID
    published_at: datetime
    items_count: int


# event_type -> (pydantic model, event_version); same contract as order.py's table.
EVENT_DATA_MODELS: dict[str, tuple[type[BaseModel], int]] = {
    "menu.published": (MenuPublishedData, 1),
}
