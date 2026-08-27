"""Starting the order saga, and relaying an event into one already running.

Internal-key only — the Order Service is the only caller, and it calls these instead of
holding a Temporal client itself, which after D36 it no longer does. Both routes mirror
what `order/saga.py` did in-process before the split; see
`order/clients/orchestrator.py` for the client that replaced it.

Lives at `apis/order.py`, not `apis/sagas.py`: a future second entity gets its own
`apis/<entity>.py` beside this one, each naming the routes for the saga *that entity*
starts and signals — see `schemas/order.py` for why the request/response models carry the
same `OrderSaga*` naming.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from common.auth import require_internal
from common.config import ORDER_TASK_QUEUE
from common.errors import conflict, not_found, service_unavailable
from common.responses import Envelope, ok
from common.temporal import workflow_id_for
from orchestrator import deps
from orchestrator.schemas.order import (
    OrderSagaSignalRequest,
    OrderSagaSignalResponse,
    OrderSagaStartRequest,
    OrderSagaStartResponse,
)
from orchestrator.workflows.order import OrderWorkflow

router = APIRouter(prefix="/api/v1/orchestrator", dependencies=[Depends(require_internal)])


@router.post(
    "/sagas",
    response_model=Envelope[OrderSagaStartResponse],
    status_code=status.HTTP_201_CREATED,
)
async def start_saga(payload: OrderSagaStartRequest) -> Envelope[OrderSagaStartResponse]:
    """Start the saga for a committed order, or hand back the one already running.

    `USE_EXISTING` is what makes this safe to call more than once: the workflow id is
    derived from the order id, so a retried checkout — the idempotent-replay branch in
    `order/apis/checkout.py` — names the same workflow and Temporal returns the running
    one rather than raising. There is deliberately no `except WorkflowAlreadyStartedError`
    for the same reason.

    An unreachable orchestrator answers `503` here — a real error, unlike the in-process
    version this replaced, which could only log. The caller (`order/clients/orchestrator.py`)
    catches it and swallows it, exactly as `order/saga.py` did before the split: the order
    is already committed, and failing the client's request over an orchestration hiccup
    would contradict D09's argument that a write that already succeeded must not be
    reported as a failure.
    """
    if not deps.temporal.connected:
        raise service_unavailable("Temporal is unreachable; the saga was not started")

    order_id = payload.order_id
    handle = await deps.temporal.client.start_workflow(
        OrderWorkflow.run,
        {
            "order_id": str(order_id),
            "restaurant_id": str(payload.restaurant_id),
            "amount": payload.amount,
            "capacity": payload.capacity,
            "restaurant_latitude": payload.restaurant_latitude,
            "restaurant_longitude": payload.restaurant_longitude,
        },
        id=workflow_id_for(order_id),
        task_queue=ORDER_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    deps.logger.info("Saga %s running for order %s", handle.id, order_id)
    return ok(
        OrderSagaStartResponse(workflow_id=handle.id, order_id=order_id),
        message="Saga started",
        status=201,
    )


@router.post("/sagas/{order_id}/signals", response_model=Envelope[OrderSagaSignalResponse])
async def signal_saga(
    order_id: UUID, payload: OrderSagaSignalRequest
) -> Envelope[OrderSagaSignalResponse]:
    """Relay an event into this order's running saga.

    `restaurant_decision` reaches here from `kitchen.py`'s accept/reject; `rider_pickup`
    and `rider_delivery` are relayed through the Order Service's own `POST
    /orders/{id}/signals`, which a sibling calls without knowing an orchestrator exists at
    all (D36's whole point). No handle is stored anywhere — the workflow id is a pure
    function of the order id, so finding the running saga costs nothing to remember.
    """
    handle = deps.temporal.client.get_workflow_handle(workflow_id_for(order_id))
    try:
        await handle.signal(payload.signal, payload.payload)
    except RPCError as exc:
        # NOT_FOUND covers both "no such workflow" and "it already finished" — the same
        # ambiguity `order/apis/signals.py` used to resolve for a caller with no way to
        # tell them apart either.
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
    return ok(
        OrderSagaSignalResponse(signalled=payload.signal, order_id=order_id),
        message="Signal delivered",
    )
