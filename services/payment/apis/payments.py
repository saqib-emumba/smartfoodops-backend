"""Customer-facing routes for the `payments` resource: charge and read.

The health probe lives in health.py instead. Two routes here charge on the customer's own
bearer token; the saga's routes, on the internal key, live in saga.py — same split
rider/apis/ makes between its own audiences.
"""

from functools import partial
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status

from common.auth import CurrentUser, require_role
from common.errors import bad_request, not_found
from common.responses import Envelope, REPLAY_RESPONSE, ok
from payment import deps
from payment.authorise import authorise
from payment.schemas.payments import PaymentCreateRequest, PaymentResponse

router = APIRouter(prefix="/api/v1/payments")


@router.post(
    "",
    response_model=Envelope[PaymentResponse],
    status_code=status.HTTP_201_CREATED,
    responses=REPLAY_RESPONSE,
)
def process_payment(
    payload: PaymentCreateRequest,
    response: Response,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("customer")),
) -> Envelope[PaymentResponse]:
    """Authorise a payment for an order, at most once per idempotency key.

    Ownership is not checked here: the Order Service lookup runs as the caller and refuses
    an order that is not theirs, so there is one place that decides it.
    """
    # The header is what a retrying client resends; the body repeats the value so the key
    # that gets persisted is explicit in the contract. A disagreement between the two means
    # the caller does not know which transaction it is retrying, so neither do we.
    if not x_idempotency_key:
        raise bad_request("X-Idempotency-Key header is required")
    if x_idempotency_key != payload.idempotency_key:
        raise bad_request(
            "X-Idempotency-Key header does not match idempotency_key in the body"
        )

    payment, replayed = authorise(
        payload,
        idempotency_key=x_idempotency_key,
        amount=payload.amount,
        # Reads the order as the caller, which is what enforces ownership.
        fetch_order=partial(deps.order_service.fetch_order, token=current_user.token),
        verify_replay=True,
        payments=deps.payments,
        gateway=deps.gateway,
        logger=deps.logger,
        replay_log="Idempotent replay for key %s",
    )
    if replayed:
        response.status_code = status.HTTP_200_OK
        return ok(PaymentResponse(**payment), message="Replayed", status=200)
    return ok(PaymentResponse(**payment), message="Payment authorised", status=201)


@router.get("/{payment_id}", response_model=Envelope[PaymentResponse])
def get_payment(
    payment_id: UUID,
    current_user: CurrentUser = Depends(require_role("customer")),
) -> Envelope[PaymentResponse]:
    """Expose a payment's state so a saga (or an operator) can see where it stopped.

    `payments` holds no customer column — the order is what knows who this belongs to — so
    ownership is settled by reading that order as the caller, the same check the charging
    path relies on.
    """
    row = deps.payments.find(payment_id)
    if row is None:
        raise not_found(f"Payment {payment_id} not found")

    deps.order_service.fetch_order(row["order_id"], current_user.token)
    return ok(PaymentResponse(**row), message="Payment found")
