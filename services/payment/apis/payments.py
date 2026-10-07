"""Customer-facing routes for the `payments` resource: charge and read.

The health probe lives in health.py instead. Two routes here charge on the customer's own
bearer token; the saga's routes, on the internal key, live in saga.py — same split
rider/apis/ makes between its own audiences.

D53/outbox-removal-temporal-design.md ss4b: `process_payment` no longer writes directly. It
starts `PaymentWorkflow` (manual mode, unified with the saga's own payment step by D55) and
waits for it to finish — the write and the publish both happen inside Temporal now, since
removing `payment_outbox` left this route's write with no activity to chain a publish step
off of otherwise. The route's own contract (status codes, body shape, 200-on-replay) is
unchanged.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status

from common.auth import CurrentUser, require_permission
from common.errors import bad_request, not_found
from common.responses import Envelope, REPLAY_RESPONSE, ok
from payment import deps
from payment.schemas.payments import PaymentCreateRequest, PaymentResponse

router = APIRouter(prefix="/api/v1/payments")


@router.post(
    "",
    response_model=Envelope[PaymentResponse],
    status_code=status.HTTP_201_CREATED,
    responses=REPLAY_RESPONSE,
)
async def process_payment(
    payload: PaymentCreateRequest,
    response: Response,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_permission("payment:create")),
) -> Envelope[PaymentResponse]:
    """Authorise a payment for an order, at most once per idempotency key.

    Ownership is not checked here directly any more — it happens inside `PaymentWorkflow`'s
    own write, as the caller, exactly as it always has (D52's model: `current_user` travels
    into the workflow as plain data, not a credential, and the write re-asserts the identity
    this request already verified).
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

    result = await deps.payment_saga.create_manual_payment(
        payload, idempotency_key=x_idempotency_key, current_user=current_user
    )
    if not result["created"]:
        response.status_code = status.HTTP_200_OK
        return ok(PaymentResponse(**result["payment"]), message="Replayed", status=200)
    return ok(PaymentResponse(**result["payment"]), message="Payment authorised", status=201)


@router.get("/{payment_id}", response_model=Envelope[PaymentResponse])
def get_payment(
    payment_id: UUID,
    current_user: CurrentUser = Depends(require_permission("payment:read")),
) -> Envelope[PaymentResponse]:
    """Expose a payment's state so a saga (or an operator) can see where it stopped.

    `payments` holds no customer column — the order is what knows who this belongs to — so
    ownership is settled by reading that order as the caller, the same check the charging
    path relies on.
    """
    row = deps.payments.find(payment_id)
    if row is None:
        raise not_found(f"Payment {payment_id} not found")

    deps.order_service.fetch_order(row["order_id"], current_user)
    return ok(PaymentResponse(**row), message="Payment found")
