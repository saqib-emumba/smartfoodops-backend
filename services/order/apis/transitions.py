"""Internal-only: a sibling records a status transition this service commits.

Reached exclusively by the orchestrator's activities, over HTTP — before D36 this was
`transition_order_activity` calling `OrderRepository.transition` in-process, because the
worker shared this service's image and database. It no longer does, so the write that used
to be a Python call is now a request, carrying the same arguments the call always took.

The only route this router carries is internal-key only, so the guard sits on the router
itself — same shape as signals.py.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from common.auth import require_internal
from common.errors import conflict, not_found
from common.responses import Envelope, ok
from order import deps
from order.repositories.orders import AtCapacity
from order.schemas.transitions import TransitionRequest, TransitionResponse

router = APIRouter(prefix="/api/v1/orders", dependencies=[Depends(require_internal)])


@router.post("/{order_id}/transitions", response_model=Envelope[TransitionResponse])
def record_transition(
    order_id: UUID, payload: TransitionRequest
) -> Envelope[TransitionResponse]:
    """Advance an order's status and append the transition, in one transaction.

    `AtCapacity` answers `409` rather than the `422`/`403` this service uses elsewhere for
    a business refusal: the orchestrator's client (`orchestrator/clients/order.py`) passes
    it through `common.service_client`'s `passthrough` mechanism specifically to preserve
    the distinction the activity needs — a full kitchen is not retryable, an unreachable
    Order Service is.
    """
    try:
        order, changed = deps.orders.transition(
            order_id=order_id,
            new_status=payload.status,
            updated_by=payload.updated_by,
            event=payload.event,
            metadata=payload.metadata,
            rider_id=payload.rider_id,
            capacity_limit=payload.capacity_limit,
            service=payload.service,
        )
    except AtCapacity as exc:
        raise conflict(str(exc)) from exc

    if order is None:
        raise not_found(f"Order {order_id} no longer exists")

    return ok(
        TransitionResponse(status=order["status"], changed=changed),
        message="Transition recorded",
    )
