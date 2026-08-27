"""Saga-facing routes for the `payments` resource — internal key only, never a bearer token.

The workflow that calls these has no user behind it: it did not choose the order it is
paying for or refunding, and a token in a workflow argument would be written into durable,
UI-visible history. Both facts are D26's, restated here because they are why this file's
routes carry no `Depends(require_role(...))` at all — see common/auth.py's `require_internal`.
"""

from fastapi import APIRouter, Depends, Response, status

from common.auth import require_internal
from common.errors import unprocessable
from payment import deps
from payment.amounts import to_cents
from payment.apis.payments import REPLAY_RESPONSE
from payment.authorise import authorise
from payment.schemas.payments import PaymentAuthorizeRequest, PaymentRefundRequest, PaymentResponse

router = APIRouter(prefix="/api/v1/payments", dependencies=[Depends(require_internal)])


@router.post(
    "/authorize",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=REPLAY_RESPONSE,
)
def authorize_for_saga(
    payload: PaymentAuthorizeRequest, response: Response
) -> PaymentResponse:
    """Authorise a payment on behalf of the order saga.

    The same steps as `payments.process_payment`, on the internal key instead of a
    customer token. Since Week 2 this is how an order actually gets paid: the workflow
    authorises, and the customer-facing endpoint is left for direct and manual use (D30).

    Dropping `require_role("customer")` does not drop the ownership guarantee, it relocates
    it. The workflow did not choose this order — it was started by an already-authorised
    `POST /api/v1/orders` whose handler had already established that the caller owns it. By
    the time an activity runs there is no user in the request at all, which is exactly why
    a forwarded bearer token could not have worked here (D26).

    Idempotency needs no header: the key is derived from the order id by the workflow, so a
    retried activity presents the same key and collides on the unique index rather than
    charging a second time.

    `amount` arrives as a string so it can become an exact Decimal here (D07).
    """
    payment, replayed = authorise(
        payload,
        idempotency_key=payload.idempotency_key,
        amount=payload.amount,
        fetch_order=deps.order_service.fetch_order_internally,
        verify_replay=False,
        payments=deps.payments,
        gateway=deps.gateway,
        logger=deps.logger,
        replay_log="Idempotent saga replay for key %s",
    )
    if replayed:
        response.status_code = status.HTTP_200_OK
    return PaymentResponse(**payment)


@router.post("/refund", response_model=PaymentResponse)
def refund_for_saga(payload: PaymentRefundRequest) -> PaymentResponse:
    """Release a hold the saga can no longer honour — its compensating action.

    Idempotent by status rather than by key, and that distinction matters: Temporal retries
    this until it succeeds, and a second refund is real money leaving. A payment already
    `refunded` is returned unchanged without touching the gateway.

    A payment still `pending` is refunded too. Those are the rows stranded when a gateway
    call failed after the intent was recorded — the ones D10 knowingly accepted and nothing
    in the platform has ever cleaned up. This is what sweeps them.

    An order with no payment at all is `422`: the saga is compensating a step that never
    completed, which is not an error in the request but is worth naming rather than
    silently reporting success.
    """
    # Read first, so the gateway is told the real amount and so the two "nothing to do"
    # cases are separated before any side effect rather than inferred after one.
    existing = deps.payments.find_by_order(payload.order_id)
    if existing is None:
        raise unprocessable(f"Order {payload.order_id} has no payment to refund")
    if existing["status"] == "refunded":
        deps.logger.info(
            "Payment for order %s was already refunded", payload.order_id
        )
        return PaymentResponse(**existing)

    refund = deps.gateway.refund(
        order_id=payload.order_id,
        amount=to_cents(existing["amount"]),
        idempotency_key=f"wf-refund-{payload.order_id}",
    )
    refunded = deps.payments.mark_refunded(payload.order_id, refund.reference)
    if refunded is None:
        # A concurrent refund won the race between the read above and this update. Its
        # result is the correct answer, so return that rather than failing the activity.
        deps.logger.info("Concurrent refund resolved order %s", payload.order_id)
        return PaymentResponse(**deps.payments.find_by_order(payload.order_id))

    deps.logger.info(
        "Refunded payment for order %s (%s)",
        payload.order_id,
        payload.reason or "no reason given",
    )
    return PaymentResponse(**refunded)
