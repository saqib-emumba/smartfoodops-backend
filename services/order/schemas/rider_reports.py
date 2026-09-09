"""What the Rider Service records against an order before it signals the saga."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class RiderReportRequest(BaseModel):
    """A pickup or delivery the Rider Service observed, on its way to a durable column.

    `stage` is the value stored in `orders.rider_reported_stage`, not a Temporal signal
    name. Until D47 this model carried `signal: Literal["rider_pickup", "rider_delivery"]`
    and the route mapped it to a stage before relaying to the orchestrator; the relay is
    gone — the Rider Service signals Temporal itself — so what crosses this boundary is
    only the fact being recorded, named as the column names it.

    Constrained to the two stages the enum allows, so a typo is a 422 here rather than a
    write that fails deeper down. `restaurant_decision` has no counterpart here and never
    did: since D32 a kitchen decision arrives through
    `POST /api/v1/orders/{id}/accept|reject`, authenticated as the owning admin, and a
    second unauthenticated path to the same fact is exactly the drift D16 warns about.
    """

    stage: Literal["picked_up", "delivered"]


class RiderReportAcceptedResponse(BaseModel):
    """The acknowledgement, not a resource — hence field names that name what was recorded
    rather than anything about the order's wider state.
    """

    recorded: str
    order_id: UUID
