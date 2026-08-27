"""The kitchen queue and the accept/reject decision — restaurant-facing.

`decide_kitchen` is a plain function here rather than in saga.py: it is called by exactly
the two routes below and nowhere else, so it stays next to its only callers.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, require_role
from common.errors import not_found
from order import deps
from order.saga import signal_saga_best_effort
from order.schemas.kitchen import KitchenDecisionResponse, KitchenOrderResponse

router = APIRouter(prefix="/api/v1/orders")


@router.get("/kitchen/{restaurant_id}", response_model=list[KitchenOrderResponse])
def kitchen_queue(
    restaurant_id: UUID,
    current_user: CurrentUser = Depends(require_role("restaurant_admin")),
) -> list[KitchenOrderResponse]:
    """The kitchen's rail: this restaurant's orders awaiting a decision, oldest first.

    Restaurant-facing, on this service, because since D32 the kitchen's queue *is* a query
    over `orders` — there is no separate ticket table to read. Whether the caller owns the
    restaurant is still the Restaurant Service's fact, resolved over HTTP (D16).

    Answers `KitchenOrderResponse`, not `OrderResponse`: an admin sees what to cook and not
    what the customer paid. Widening the queue onto `orders` deliberately did not widen what
    a restaurant can read.
    """
    deps.restaurant_service.verify_owner(restaurant_id, current_user)
    return [
        KitchenOrderResponse(**row) for row in deps.orders.kitchen_queue(restaurant_id)
    ]


async def _decide_kitchen(
    order_id: UUID, current_user: CurrentUser, decision: str
) -> KitchenDecisionResponse:
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
    if not changed:
        deps.logger.info(
            "Order %s is already '%s'/%s; not signalling the saga again",
            order_id,
            decided["status"],
            decided["kitchen_decision"],
        )
        return KitchenDecisionResponse(
            order_id=order_id,
            decision=decided["kitchen_decision"],
            status=decided["status"],
            changed=False,
        )

    await signal_saga_best_effort(
        order_id, "restaurant_decision", {"decision": decision}
    )
    return KitchenDecisionResponse(
        order_id=order_id,
        decision=decided["kitchen_decision"],
        status=decided["status"],
        changed=True,
    )


@router.post("/{order_id}/accept", response_model=KitchenDecisionResponse)
async def accept_order(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_role("restaurant_admin")),
) -> KitchenDecisionResponse:
    """Accept an order into the kitchen, releasing the saga to find a rider."""
    return await _decide_kitchen(order_id, current_user, "accepted")


@router.post("/{order_id}/reject", response_model=KitchenDecisionResponse)
async def reject_order(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_role("restaurant_admin")),
) -> KitchenDecisionResponse:
    """Decline an order, which makes the saga refund the customer and cancel it."""
    return await _decide_kitchen(order_id, current_user, "rejected")
