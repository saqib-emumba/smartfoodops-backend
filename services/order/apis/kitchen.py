"""The kitchen queue and the accept/reject decision — restaurant-facing.

`_decide_kitchen` is a plain function here rather than in clients/orchestrator.py: it is
called by exactly the two routes below and nowhere else, so it stays next to its only
callers.

D53/outbox-removal-temporal-design.md ss4a: accepting or rejecting now goes through a
Temporal Update rather than a direct database write followed by a best-effort signal.
Removing the outbox left this write with no activity to chain a publish step off unless the
write itself moved inside Temporal — the same reasoning that moved order creation there, on
a smaller write. Temporal being unreachable now means a restaurant cannot record a decision
at all, where before the decision always committed and only the saga's awareness of it was
best-effort.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from common.auth import CurrentUser, require_permission
from common.errors import not_found
from common.responses import Envelope, ok
from order import deps
from order.schemas.kitchen import KitchenDecisionResponse, KitchenOrderResponse

router = APIRouter(prefix="/api/v1/orders")


@router.get("/kitchen/{restaurant_id}", response_model=Envelope[list[KitchenOrderResponse]])
def kitchen_queue(
    restaurant_id: UUID,
    current_user: CurrentUser = Depends(require_permission("kitchen:read")),
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
    """Verify ownership here, on the bearer token; record the decision through Temporal.

    Ownership stays checked exactly where it always was — the Restaurant Service's fact,
    resolved as the caller (D16) — before anything reaches the workflow. What changed is
    only what happens after: a Temporal Update now does the write and the saga hand-off in
    one round trip (D53), rather than a direct write followed by a signal the saga might
    never see.

    An already-decided order is answered from this database read alone, *before* touching
    Temporal. This isn't an optimisation — it's load-bearing: by the time a second accept or
    reject on an already-decided order arrives, the saga itself may already have finished
    (an outright reject completes `OrderWorkflow.run()` via `_compensate`), and Temporal
    refuses to deliver an Update to a workflow that no longer exists. The database's own
    `kitchen_decision` column, unlike the workflow, outlives the saga — the same durability
    gap D32 already closed for reading a decision back after a timeout, applied here to
    reading it back after completion instead.
    """
    order = deps.orders.find(order_id)
    if order is None:
        raise not_found(f"Order {order_id} not found")
    deps.restaurant_service.verify_owner(order["restaurant_id"], current_user)

    if order["kitchen_decision"] is not None:
        return ok(
            KitchenDecisionResponse(
                order_id=order_id,
                decision=order["kitchen_decision"],
                status=order["status"],
                changed=False,
            ),
            message="Decision unchanged",
        )

    result = await deps.orchestrator_service.decide_kitchen(order_id, decision)
    return ok(
        KitchenDecisionResponse(
            order_id=order_id,
            decision=result["decision"],
            status=result["status"],
            changed=result["changed"],
        ),
        message="Decision recorded" if result["changed"] else "Decision unchanged",
    )


@router.post("/{order_id}/accept", response_model=Envelope[KitchenDecisionResponse])
async def accept_order(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_permission("kitchen:decide")),
) -> Envelope[KitchenDecisionResponse]:
    """Accept an order into the kitchen, releasing the saga to find a rider."""
    return await _decide_kitchen(order_id, current_user, "accepted")


@router.post("/{order_id}/reject", response_model=Envelope[KitchenDecisionResponse])
async def reject_order(
    order_id: UUID,
    current_user: CurrentUser = Depends(require_permission("kitchen:decide")),
) -> Envelope[KitchenDecisionResponse]:
    """Decline an order, which makes the saga refund the customer and cancel it."""
    return await _decide_kitchen(order_id, current_user, "rejected")
