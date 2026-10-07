"""Internal-only: where an order is actually created now (D53/
order-creation-temporal-update-design.md).

Reached exclusively by the order-creation saga's `create_order_activity`, the first step of
`OrderWorkflow.run()` (D58; until then it ran inside a `create_order` Update) — before this,
`checkout.py::create_order` ran all of this directly as a synchronous HTTP handler. Re-pricing, customer/restaurant verification, and
the insert all moved here unchanged; only the caller changed, from "an HTTP request" to "a
Temporal activity".

Internal-key only, same convention `transitions.py` and `/orders/{id}/internal` already use:
without a bearer token there is no ownership check left on this route by itself, so it must not
be reachable by anyone who is not already a sibling service.
"""

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, require_internal
from common.errors import conflict
from common.responses import Envelope, ok
from order import deps
from order.pricing import build_order_snapshot
from order.repositories.orders import AtCapacity
from order.schemas.orders import (
    OrderCreateRequest,
    OrderInternalCreateRequest,
    OrderInternalCreateResponse,
    OrderResponse,
)

router = APIRouter(prefix="/api/v1/orders", dependencies=[Depends(require_internal)])


@router.post("/internal/create", response_model=Envelope[OrderInternalCreateResponse])
def create_order_internally(
    payload: OrderInternalCreateRequest,
) -> Envelope[OrderInternalCreateResponse]:
    """Re-price, verify, and insert — the write `checkout.py` used to make directly.

    `current_user` is reconstructed from the payload rather than taken from a request
    header: the caller is a Temporal activity, which has no bearer token to forward, only
    the identity the original `POST /orders` request already verified and carried into the
    workflow (D52's model, applied to a workflow argument instead of an HTTP header).
    """
    current_user = CurrentUser(user_id=payload.customer_id, roles=payload.customer_roles)

    cart = OrderCreateRequest(
        restaurant_id=payload.restaurant_id,
        items=payload.items,
        idempotency_key=payload.idempotency_key,
    )
    menu = deps.menu_service.fetch_menu(payload.restaurant_id, current_user)
    items_snapshot, total = build_order_snapshot(menu, cart)

    deps.user_service.verify_customer(payload.customer_id, current_user)
    restaurant = deps.restaurant_service.verify_restaurant(payload.restaurant_id, current_user)

    # The early capacity check (D58): turn a full kitchen away before anything is written or
    # charged. `409` rides the same passthrough the activity already maps for a key
    # collision (`OrderCreateConflict`, non-retryable), so the customer gets the message
    # below as a `409`. The authoritative check is still the one at `confirmed`.
    try:
        order, created = deps.orders.create(
            payload.order_id,
            cart,
            payload.customer_id,
            items_snapshot,
            total,
            payload.idempotency_key,
            capacity_limit=restaurant["capacity"],
        )
    except AtCapacity as exc:
        raise conflict(
            "The kitchen is at capacity right now; please try again shortly"
        ) from exc

    return ok(
        OrderInternalCreateResponse(
            order=OrderResponse(**order),
            created=created,
            capacity=restaurant["capacity"],
            restaurant_latitude=restaurant["latitude"],
            restaurant_longitude=restaurant["longitude"],
        ),
        message="Order created" if created else "Order already existed",
    )
