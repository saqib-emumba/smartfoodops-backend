"""Rider-facing delivery reporting: pickup and hand-over.

Neither route touches the database. Both check that this rider is carrying this order and
then relay the fact to the Order Service, which owns the lifecycle and the workflow — this
service does not know Temporal exists.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from common.auth import CurrentUser, require_role
from rider import deps
from rider.fleet import own_profile, report_event

router = APIRouter(prefix="/api/v1/riders/me/orders")


@router.post("/{order_id}/picked-up", status_code=status.HTTP_204_NO_CONTENT)
def mark_picked_up(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_role("rider")),
) -> None:
    """Report collecting an order from the kitchen."""
    report_event(
        deps.order_service,
        own_profile(deps.riders, current_user),
        order_id,
        "rider_pickup",
    )


@router.post("/{order_id}/delivered", status_code=status.HTTP_204_NO_CONTENT)
def mark_delivered(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_role("rider")),
) -> None:
    """Report handing an order to the customer.

    The rider is *not* released here. The saga releases them, in the same step that records
    the `delivered` transition, so availability and order state can never disagree — and so
    a delivery reported for a workflow that has already been cancelled does not hand a rider
    back twice.
    """
    report_event(
        deps.order_service,
        own_profile(deps.riders, current_user),
        order_id,
        "rider_delivery",
    )
