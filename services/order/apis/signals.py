"""Relaying an event from a sibling service into the order's workflow.

The only route this router carries is internal-key only, so the guard sits on the router
itself — the same shape as payment/apis/saga.py and rider/apis/dispatch.py, both entirely
one audience. tracking.py can't do the same: its two routes carry different credentials.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from common.auth import require_internal
from common.responses import Envelope, ok
from order import deps
from order.schemas.signals import SignalAcceptedResponse, WorkflowSignalRequest

router = APIRouter(prefix="/api/v1/orders", dependencies=[Depends(require_internal)])


@router.post(
    "/{order_id}/signals",
    response_model=Envelope[SignalAcceptedResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def signal_workflow(
    order_id: UUID, payload: WorkflowSignalRequest
) -> Envelope[SignalAcceptedResponse]:
    """Relay an event from a sibling service into this order's workflow.

    One endpoint rather than one per event, and internal-key only. Since D36, no process in
    this service holds a Temporal client at all — this is now a thin proxy into the
    Orchestrator Service's own relay, which does the actual lookup and the `404`/`409`
    mapping (see `order/clients/orchestrator.py::signal` and
    `orchestrator/apis/sagas.py::signal_saga`). The Restaurant and Rider Services stay
    unaware any of this exists: they report what they observed to the service that owns the
    order lifecycle, exactly as they would report any other status transition, and it is
    this service's job — not theirs — to know who runs the saga.

    `202`, not `200`: a signal is delivered to the workflow, not executed by it. By the time
    this returns the saga has been told, not necessarily acted.
    """
    await deps.orchestrator_service.signal(order_id, payload.signal, payload.payload)
    deps.logger.info("Signalled '%s' to the saga for order %s", payload.signal, order_id)
    return ok(
        SignalAcceptedResponse(signalled=payload.signal, order_id=order_id),
        message="Signal accepted",
        status=202,
    )
