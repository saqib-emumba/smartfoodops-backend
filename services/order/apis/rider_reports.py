"""Recording what the Rider Service observed about an order.

The only route this router carries is internal-key only, so the guard sits on the router
itself — the same shape as payment/apis/saga.py and rider/apis/dispatch.py, both entirely
one audience. tracking.py can't do the same: its two routes carry different credentials.

Until D47 this file was `signals.py` and did two things: record the stage, then relay a
signal into the saga through the Orchestrator Service. Only the first half is left. The
Rider Service holds its own Temporal client now and sends the signal itself, so this is a
plain write to the column the saga reads back — which is what it always was underneath.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from common.auth import require_internal
from common.responses import Envelope, ok
from order import deps
from order.schemas.rider_reports import RiderReportAcceptedResponse, RiderReportRequest

router = APIRouter(prefix="/api/v1/orders", dependencies=[Depends(require_internal)])


@router.post(
    "/{order_id}/rider-report",
    response_model=Envelope[RiderReportAcceptedResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def record_rider_report(
    order_id: UUID, payload: RiderReportRequest
) -> Envelope[RiderReportAcceptedResponse]:
    """Record a pickup or delivery the rider reported, against the order it belongs to.

    Why this crosses a service boundary at all: the Rider Service observed the fact, but
    an order's state is the Order Service's to hold (D01), and the column this writes —
    `orders.rider_reported_stage` — is what `read_rider_report_activity` reads when the
    saga's delivery timer expires. Keeping it here keeps that read local to the service the
    saga already asks about orders, and keeps the report per-order rather than per-rider.

    **The caller must await this before signalling, never after.** The whole value of the
    column is that it survives a signal that never lands: a timeout can then tell "the rider
    genuinely never reported" apart from "they reported and the signal was lost" (D43 —
    much of the Week 3 code cites this as "D46", a miscitation; the record is D43).
    Signal-then-record inverts that and leaves nothing to recover from.

    `202`, not `200`: the report is recorded, not acted on. What acts on it is the saga, and
    it may not have been told yet — the caller signals immediately after this returns.

    A repeat report is a no-op rather than an error. `record_rider_report`'s guard is
    forward-only, so a retried report for a stage already recorded — or an older stage
    arriving after a newer one — changes nothing and still answers `202`.
    """
    deps.orders.record_rider_report(order_id, payload.stage)
    deps.logger.info("Recorded rider report '%s' for order %s", payload.stage, order_id)
    return ok(
        RiderReportAcceptedResponse(recorded=payload.stage, order_id=order_id),
        message="Rider report recorded",
        status=202,
    )
