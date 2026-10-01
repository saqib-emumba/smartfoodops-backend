"""Internal-only: the write behind the `kitchen_decision` Temporal Update (D53).

Before D53 this write lived directly in `kitchen.py`, reached by the restaurant's own bearer
token, with the saga told about it afterward via a best-effort signal. Removing the outbox
left nothing to chain a publish activity off unless the write itself moved inside Temporal, so
`kitchen.py` now calls a Temporal Update instead, and this endpoint — internal-key only, like
every other route in this module family — is what that Update's `decide_kitchen_activity`
calls to actually record the decision.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from common.auth import require_internal
from common.errors import not_found
from common.responses import Envelope, ok
from order import deps
from order.schemas.kitchen import KitchenDecisionRequest, KitchenDecisionResponse

router = APIRouter(prefix="/api/v1/orders", dependencies=[Depends(require_internal)])


@router.post(
    "/{order_id}/internal/kitchen-decision", response_model=Envelope[KitchenDecisionResponse]
)
def record_kitchen_decision(
    order_id: UUID, payload: KitchenDecisionRequest
) -> Envelope[KitchenDecisionResponse]:
    decided, changed = deps.orders.decide_kitchen(order_id, payload.decision)
    if decided is None:
        raise not_found(f"Order {order_id} no longer exists")
    return ok(
        KitchenDecisionResponse(
            order_id=order_id,
            decision=decided["kitchen_decision"],
            status=decided["status"],
            changed=changed,
        ),
        message="Decision recorded" if changed else "Decision unchanged",
    )
