"""The one write the orchestrator makes into this service — a status transition, reached
over HTTP now that Temporal runs in its own deployable that touches no database (D36).
"""

from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class TransitionRequest(BaseModel):
    """What `OrderRepository.transition` already took as keyword arguments, on the wire.

    `service` names the calling process — `orchestrator-worker` today — and is stamped
    into `order_tracking_logs.service` instead of this service's own identity, so the trail
    keeps saying who actually observed the transition (see `transition`'s docstring).
    """

    status: str
    updated_by: str = "system"
    service: str
    event: Optional[dict] = None
    metadata: Optional[dict] = None
    rider_id: Optional[UUID] = None
    capacity_limit: Optional[int] = None


class TransitionResponse(BaseModel):
    """The same shape `transition_order_activity` already returned when the call was
    in-process — a status and whether it actually changed, not the order itself."""

    status: str
    changed: bool
