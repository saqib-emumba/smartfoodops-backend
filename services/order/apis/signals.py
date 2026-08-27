"""Relaying an event from a sibling service into the order's workflow."""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from temporalio.service import RPCError, RPCStatusCode

from common.auth import require_internal
from common.errors import conflict, not_found
from common.temporal import workflow_id_for
from order import deps
from order.schemas.signals import WorkflowSignalRequest

router = APIRouter(prefix="/api/v1/orders")


@router.post(
    "/{order_id}/signals",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_internal)],
)
async def signal_workflow(order_id: UUID, payload: WorkflowSignalRequest) -> dict:
    """Relay an event from a sibling service into this order's workflow.

    One endpoint rather than one per event, and internal-key only. Two consequences worth
    stating: a Temporal client exists in exactly two processes in this platform — this
    service and its worker — and the Restaurant and Rider Services stay unaware that an
    orchestrator exists at all. They report what they observed to the service that owns the
    order lifecycle, exactly as they would report any other status transition.

    No handle is stored anywhere. The workflow id is derived from the order id, so finding
    the running saga is a pure function of the thing the caller already named.

    `202`, not `200`: a signal is delivered to the workflow, not executed by it. By the time
    this returns the saga has been told, not necessarily acted.
    """
    handle = deps.temporal.client.get_workflow_handle(workflow_id_for(order_id))
    try:
        await handle.signal(payload.signal, payload.payload)
    except RPCError as exc:
        # NOT_FOUND covers both "no such workflow" and "it already finished", and the two
        # are worth separating for the caller: a rider marking a cancelled order delivered
        # is a different problem from an order that never existed.
        if exc.status is RPCStatusCode.NOT_FOUND:
            raise not_found(
                f"Order {order_id} has no running saga to signal; it may have already "
                "finished or been cancelled"
            ) from exc
        deps.logger.error("Could not signal saga for order %s: %s", order_id, exc)
        raise conflict(
            f"The saga for order {order_id} would not accept signal '{payload.signal}'"
        ) from exc

    deps.logger.info("Signalled '%s' to the saga for order %s", payload.signal, order_id)
    return {"signalled": payload.signal, "order_id": str(order_id)}
