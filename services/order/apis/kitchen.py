"""The kitchen queue and the accept/reject decision — restaurant-facing.

`_decide_kitchen` is a plain function here rather than in clients/orchestrator.py: it is
called by exactly the two routes below and nowhere else, so it stays next to its only
callers. The signal hand-off itself is one line into that client, unchanged in shape since
before D36 — only which process answers it changed.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, require_role
from common.errors import not_found
from common.responses import Envelope, ok
from order import deps
from order.schemas.kitchen import KitchenDecisionResponse, KitchenOrderResponse

router = APIRouter(prefix="/api/v1/orders")


@router.get("/kitchen/{restaurant_id}", response_model=Envelope[list[KitchenOrderResponse]])
def kitchen_queue(
    restaurant_id: UUID,
    current_user: CurrentUser = Depends(require_role("restaurant_admin")),
) -> Envelope[list[KitchenOrderResponse]]:
    """The kitchen's rail: this restaurant's orders awaiting a decision, oldest first.

    Restaurant-facing, on this service, because since D32 the kitchen's queue *is* a query
    over `orders` — there is no separate ticket table to read. Whether the caller owns the
    restaurant is still the Restaurant Service's fact, resolved over HTTP (D16).

    Answers `KitchenOrderResponse`, not `OrderResponse`: an admin sees what to cook and not
    what the customer paid. Widening the queue onto `orders` deliberately did not widen what
    a restaurant can read.
    """
    deps.restaurant_service.verify_owner(restaurant_id, current_user)
    rows = [
        KitchenOrderResponse(**row) for row in deps.orders.kitchen_queue(restaurant_id)
    ]
    return ok(rows, message="Kitchen queue")


async def _decide_kitchen(
    order_id: UUID, current_user: CurrentUser, decision: str
) -> Envelope[KitchenDecisionResponse]:
    """Record a kitchen decision and tell the saga about it, exactly once.

    Two properties this ordering buys, and both matter:

    The decision is committed *before* the signal, so a signal that fails to send leaves a
    decision on record rather than losing it — and the saga reads that record back when its
    timer expires, which is what makes a lost signal self-correcting.

    The signal is sent only when the update actually changed something. A second accept must
    not tell the workflow twice, and a click on an order the saga already timed out and
    cancelled must not signal at all.

    Since D32 this service owns both halves: the decision is a column in its own database
    and the workflow is its own, so there is no cross-service relay left to lose.
    """
    order = deps.orders.find(order_id)
    if order is None:
        raise not_found(f"Order {order_id} not found")
    deps.restaurant_service.verify_owner(order["restaurant_id"], current_user)

    decided, changed = deps.orders.decide_kitchen(order_id, decision)
    if decided is None:
        # The order was found above but is gone by the time `decide_kitchen` re-reads it —
        # a race this narrow has no test covering it and, before the envelope pass gave
        # every route a matching exception handler to fall through to, crashed with a bare
        # `TypeError` on `None["status"]` instead of a clean 404. See payment/apis/saga.py's
        # `refund_for_saga` for the same shape of fix.
        raise not_found(f"Order {order_id} no longer exists")
    if not changed:
        deps.logger.info(
            "Order %s is already '%s'/%s; not signalling the saga again",
            order_id,
            decided["status"],
            decided["kitchen_decision"],
        )
        return ok(
            KitchenDecisionResponse(
                order_id=order_id,
                decision=decided["kitchen_decision"],
                status=decided["status"],
                changed=False,
            ),
            message="Decision unchanged",
        )

    await deps.orchestrator_service.signal_saga_best_effort(
        order_id, "restaurant_decision", {"decision": decision}
    )
    return ok(
        KitchenDecisionResponse(
            order_id=order_id,
            decision=decided["kitchen_decision"],
            status=decided["status"],
            changed=True,
        ),
        message="Decision recorded",
    )


@router.post("/{order_id}/accept", response_model=Envelope[KitchenDecisionResponse])
async def accept_order(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_role("restaurant_admin")),
) -> Envelope[KitchenDecisionResponse]:
    """Accept an order into the kitchen, releasing the saga to find a rider."""
    return await _decide_kitchen(order_id, current_user, "accepted")


@router.post("/{order_id}/reject", response_model=Envelope[KitchenDecisionResponse])
async def reject_order(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_role("restaurant_admin")),
) -> Envelope[KitchenDecisionResponse]:
    """Decline an order, which makes the saga refund the customer and cancel it."""
    return await _decide_kitchen(order_id, current_user, "rejected")
