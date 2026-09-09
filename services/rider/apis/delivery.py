"""Rider-facing delivery reporting: pickup and hand-over.

Both routes check that this rider is carrying this order, record the stage against the order
through the Order Service, and then signal the saga directly — this service holds its own
Temporal client as of D47. It previously did neither: both routes were pure relays into
`POST /orders/{id}/signals`, and the Order Service recorded the stage and forwarded the
signal on this service's behalf.

Why the write still crosses a boundary while the signal no longer does: an order's state
belongs to the Order Service (D01), so `orders.rider_reported_stage` stays there and stays
readable by the saga's own recovery activity. The signal is not state — it is an event
addressed to a workflow, and the service that observed it is the honest sender.

`async def` because the signal is awaited. `own_profile` and the record call remain
blocking; both are fast, and making them async would mean an async repository this service
has no other use for.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, require_role
from common.responses import Envelope, ok
from rider import deps
from rider.fleet import own_profile, report_event

router = APIRouter(prefix="/api/v1/riders/me/orders")


@router.post("/{order_id}/picked-up", response_model=Envelope[None])
async def mark_picked_up(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_role("rider")),
) -> Envelope[None]:
    """Report collecting an order from the kitchen.

    Answers `200` with a `null` body rather than the platform's old `204` — see
    user/apis/sessions.py's logout for the same change and the reason.
    """
    await report_event(
        deps.order_service,
        deps.saga,
        own_profile(deps.riders, current_user),
        order_id,
        stage="picked_up",
        signal="rider_pickup",
    )
    return ok(message="Pickup reported")


@router.post("/{order_id}/delivered", response_model=Envelope[None])
async def mark_delivered(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_role("rider")),
) -> Envelope[None]:
    """Report handing an order to the customer.

    Answers `200` with a `null` body — same 204-to-200 change as `mark_picked_up`.

    The rider is *not* released here. The saga releases them, in the same step that records
    the `delivered` transition, so availability and order state can never disagree — and so
    a delivery reported for a workflow that has already been cancelled does not hand a rider
    back twice.
    """
    await report_event(
        deps.order_service,
        deps.saga,
        own_profile(deps.riders, current_user),
        order_id,
        stage="delivered",
        signal="rider_delivery",
    )
    return ok(message="Delivery reported")
