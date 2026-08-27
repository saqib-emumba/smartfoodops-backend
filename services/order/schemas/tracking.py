"""The append-only audit trail, both the write a sibling sends and the read a caller gets."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class OrderTrackingLogCreateRequest(BaseModel):
    """One reported state transition, on the wire.

    Unchanged from the shape the Menu Service accepted while the trail lived in MongoDB,
    so the move to `order_tracking_logs` cost callers nothing. `status` is validated
    against the `order_status` enum by the database rather than restated here — there is
    one list of valid statuses and it is the one the `orders` table already uses.
    """

    order_id: UUID
    status: str
    service: str
    raw_log: str
    updated_by: Optional[str] = "system"
    metadata: Optional[dict] = None


class OrderTrackingLogResponse(BaseModel):
    """A persisted entry. `previous_status` is derived server-side from the entry before
    it, so a caller cannot report a transition that contradicts the recorded history."""

    id: UUID
    order_id: UUID
    previous_status: Optional[str] = None
    status: str
    service: str
    updated_by: str
    raw_log: Optional[str] = None
    metadata: dict = {}
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
