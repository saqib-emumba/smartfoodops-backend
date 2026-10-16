"""The order, its trail and its saga's progress, in one read for the AI Service (Week 5, D64).

Internal-key only and unmapped in `route_permissions`, so the gateway refuses it (D57). The
ownership check lives in the AI Service, which holds the caller's identity; this route is for a
caller that has already established it.

Why here and not in the AI Service: the trail and the order are this service's tables (D01), and
the Temporal client is already wired in `deps`, so the AI image needs neither a second database
connection nor `temporalio`.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from common.auth import require_internal
from common.errors import not_found
from common.responses import Envelope, ok
from order import deps
from order.schemas.explain import ExplainTimelineEntry, OrderExplainContext, SagaProgress

router = APIRouter(prefix="/api/v1/orders", dependencies=[Depends(require_internal)])


def _number(metadata: dict, key: str) -> float | None:
    """`metadata[key]` if it is a real number, else None. A bool is an int in Python, and a string
    in a free-form JSONB column is not a figure an explanation may quote."""
    value = metadata.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@router.get("/internal/{order_id}/explain-context", response_model=Envelope[OrderExplainContext])
async def explain_context(order_id: UUID) -> Envelope[OrderExplainContext]:
    order = deps.orders.find(order_id)
    if order is None:
        raise not_found(f"Order {order_id} not found")

    timeline = []
    for row in deps.tracking.timeline(order_id):
        metadata = row["metadata"] or {}
        assigned = row["status"] == "assigned"
        timeline.append(
            ExplainTimelineEntry(
                status=row["status"],
                previous_status=row["previous_status"],
                service=row["service"],
                reason=metadata.get("reason"),
                estimated_courier_minutes=_number(metadata, "eta_minutes") if assigned else None,
                courier_distance_at_assignment_km=_number(metadata, "distance_km") if assigned else None,
                occurred_at=row["created_at"],
            )
        )
    progress = await deps.orchestrator_service.progress(order_id)
    return ok(
        OrderExplainContext(
            order_id=order["id"],
            customer_id=order["customer_id"],
            restaurant_id=order["restaurant_id"],
            status=order["status"],
            total_amount=order["total_amount"],
            kitchen_decision=order["kitchen_decision"],
            kitchen_decided_at=deps.orders.kitchen_decided_at(order_id),
            rider_reported_stage=order["rider_reported_stage"],
            rider_assigned=order["rider_id"] is not None,
            timeline=timeline,
            saga=SagaProgress(**progress),
        ),
        message="Explanation context",
    )


@router.get("/internal/customers/{customer_id}/latest-order")
def latest_order(customer_id: UUID) -> Envelope[dict]:
    """The id of a customer's most recent order, so "why is my order late?" needs no id.

    Only the id: the assistant then asks for the full explanation context, whose ownership check
    still runs on the AI Service side. `404` when the customer has never ordered.
    """
    order_id = deps.orders.latest_id_for_customer(customer_id)
    if order_id is None:
        raise not_found(f"Customer {customer_id} has no orders")
    return ok({"order_id": str(order_id)}, message="Latest order")
