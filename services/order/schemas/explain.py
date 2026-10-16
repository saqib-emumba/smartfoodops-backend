"""What the AI Service may know about an order when asked to explain it (Week 5, D64)."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel


class ExplainTimelineEntry(BaseModel):
    """One transition, reduced to what an explanation can safely use.

    `reason` is the saga's short machine code (`no_rider_available`, `pickup_timeout`, ...) and
    nothing else from the entry's `metadata`: the free-text `detail` and the `raw_log` can carry
    technical error text, and the explanation must never repeat a stack trace to a customer.
    """

    status: str
    previous_status: Optional[str] = None
    service: str
    reason: Optional[str] = None
    # Only on the `assigned` entry: what the rider workflow recorded at dispatch. The minutes are
    # `rider.eta`'s mock estimate for the courier to reach the restaurant (rider-to-restaurant
    # distance at an assumed speed, plus padding) — an estimate made then, not a promise, and
    # nothing about the leg to the customer. Whitelisted from `metadata` one key at a time: the
    # same dict also holds `rider_id`, which must not reach a prompt (D62).
    estimated_courier_minutes: Optional[float] = None
    courier_distance_at_assignment_km: Optional[float] = None
    occurred_at: datetime


class SagaProgress(BaseModel):
    """The running workflows' own view, when they are still running. All None for an order whose
    saga has finished (or when Temporal cannot be reached) — absence, not a claim."""

    fulfillment_stage: Optional[str] = None
    rider_picked_up: Optional[bool] = None
    rider_delivered: Optional[bool] = None


class OrderExplainContext(BaseModel):
    order_id: UUID
    customer_id: UUID
    restaurant_id: UUID
    status: str
    total_amount: float
    kitchen_decision: Optional[str] = None
    # When the restaurant answered. With the trail's `confirmed` and `assigned` timestamps it splits
    # the wait into "the restaurant took N minutes" and "finding a courier took N minutes".
    kitchen_decided_at: Optional[datetime] = None
    rider_reported_stage: Optional[str] = None
    # Whether a rider holds this order — never which one.
    rider_assigned: bool
    timeline: List[ExplainTimelineEntry]
    saga: SagaProgress
