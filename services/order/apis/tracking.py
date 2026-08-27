"""The append-only audit trail: a sibling's write, and a caller's read."""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from common.auth import CurrentUser, get_current_user, require_internal, require_self_or_admin
from common.errors import not_found
from order import deps
from order.schemas.tracking import OrderTrackingLogCreateRequest, OrderTrackingLogResponse

router = APIRouter(prefix="/api/v1/orders")


@router.post(
    "/logs",
    response_model=OrderTrackingLogResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal)],
)
def log_order_status(payload: OrderTrackingLogCreateRequest) -> OrderTrackingLogResponse:
    """Append a status transition reported by another service.

    Service-to-service only, on the internal key rather than a bearer token: the Order
    Service writes its own transitions in-process, so anything arriving here is a sibling
    reporting one it observed — a rider marking a delivery, a workflow cancelling. The
    customer must not be able to call it themselves, because the audit trail cannot be
    writable by the party it is about.

    This endpoint replaced `POST /api/v1/menus/logs`; it moved with the table it writes to.

    An unknown order or an invented status is `422`: the request is well formed, and it is
    the thing it points at that is wrong.
    """
    return OrderTrackingLogResponse(**deps.tracking.append(payload))


@router.get("/{order_id}/logs", response_model=list[OrderTrackingLogResponse])
def get_order_timeline(
    order_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[OrderTrackingLogResponse]:
    """Return every recorded transition for one order, oldest first.

    Readable by the customer who placed it, or an admin — the same rule as the order
    itself, decided in the same place (D16). The order is looked up first so an unknown id
    is `404` rather than an empty list, which would otherwise be indistinguishable from an
    order that exists and has no trail.
    """
    order = deps.orders.find(order_id)
    if order is None:
        raise not_found(f"Order {order_id} not found")

    require_self_or_admin(current_user, order["customer_id"])
    return [OrderTrackingLogResponse(**row) for row in deps.tracking.timeline(order_id)]
