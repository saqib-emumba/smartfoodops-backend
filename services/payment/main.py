"""SmartFoodOps Payment Service — idempotent authorisation (Port 8005).

Owns the `payments` table in its own PostgreSQL database. It is a separate service because
card handling is the one part of the platform worth isolating on its own: the compliance
boundary shrinks to this container and its database, and a gateway outage can no longer
starve the threads that place, read and track orders.

The order a payment settles lives in the Order Service's database, so it is verified over
HTTP before the insert — see clients.py — and the amount is checked against the total that
service already recalculated from the live menu.
"""

from uuid import UUID

from fastapi import Depends, FastAPI, Header, Response, status

from amounts import assert_settles_order, to_cents
from clients import OrderServiceClient
from common.auth import Principal, require_role
from common.config import required
from common.errors import bad_request, not_found
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from gateway import MockPaymentGateway
from repository import PaymentRepository
from schemas import PaymentCreateRequest, PaymentResponse

SERVICE_NAME = "payment-service"
DATABASE_URL = required("DATABASE_URL")

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(
    DATABASE_URL,
    logger=logger,
    exhausted_detail="Database connection pool exhausted; no payment was attempted",
)
payments = PaymentRepository(db, logger=logger)
order_service = OrderServiceClient(logger)
gateway = MockPaymentGateway(logger)

app = FastAPI(title="SmartFoodOps Payment Service", lifespan=db.lifespan)


@app.get("/api/v1/payments/health")
def health():
    return {
        "status": "Payment Service is operational",
        "service": SERVICE_NAME,
        "database_reachable": db.is_reachable(),
        "order_service_url": order_service.base_url,
        "gateway": gateway.name,
    }


@app.post(
    "/api/v1/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
def process_payment(
    payload: PaymentCreateRequest,
    response: Response,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
    principal: Principal = Depends(require_role("customer")),
) -> PaymentResponse:
    """Authorise a payment for an order, at most once per idempotency key.

    Ownership is not checked here: the Order Service lookup in step (c) runs as the caller
    and refuses an order that is not theirs, so there is one place that decides it.
    """
    # (a) The header is what a retrying client resends; the body repeats the value so the
    # key that gets persisted is explicit in the contract. A disagreement between the two
    # means the caller does not know which transaction it is retrying, so neither do we.
    if not x_idempotency_key:
        raise bad_request("X-Idempotency-Key header is required")
    if x_idempotency_key != payload.idempotency_key:
        raise bad_request(
            "X-Idempotency-Key header does not match idempotency_key in the body"
        )

    # (b) Replay protection — an already-seen key returns the stored payment untouched and
    # never reaches the gateway. This is the whole double-charge guarantee.
    existing = payments.find_by_idempotency_key(x_idempotency_key)
    if existing is not None:
        # Confirm the replay comes from whoever owns the order before handing back a
        # payment record; idempotency keys are client-chosen and therefore guessable.
        order_service.fetch_order(existing["order_id"], principal.token)
        response.status_code = status.HTTP_200_OK
        logger.info("Idempotent replay for key %s", x_idempotency_key)
        return PaymentResponse(**existing)

    # (c) `payments.order_id` lost its foreign key when this table moved out of the Order
    # Service's database. The HTTP check that replaces it sits here, immediately before the
    # write, and doubles as the guard that the amount settles the order exactly.
    amount = to_cents(payload.amount)
    order = order_service.fetch_order(payload.order_id, principal.token)
    assert_settles_order(order, amount)

    # (d) Record the intent first: the insert claims the idempotency key, so a concurrent
    # retry is rejected by the unique index here rather than at the gateway.
    payment = payments.create_pending(payload, amount)

    # (e) Charge, then store what the gateway gave back. A gateway failure leaves the row
    # `pending` with nothing charged — Week 2's Temporal compensation workflow is what
    # reconciles those, which is exactly why the two steps are not one.
    authorization = gateway.authorize(
        order_id=payload.order_id, amount=amount, idempotency_key=x_idempotency_key
    )
    authorized = payments.mark_authorized(payment["id"], authorization.reference)

    return PaymentResponse(**authorized)


@app.get("/api/v1/payments/{payment_id}", response_model=PaymentResponse)
def get_payment(
    payment_id: UUID,
    principal: Principal = Depends(require_role("customer")),
) -> PaymentResponse:
    """Expose a payment's state so a saga (or an operator) can see where it stopped.

    `payments` holds no customer column — the order is what knows who this belongs to — so
    ownership is settled by reading that order as the caller, the same check step (c) of
    process_payment relies on.
    """
    row = payments.find(payment_id)
    if row is None:
        raise not_found(f"Payment {payment_id} not found")

    order_service.fetch_order(row["order_id"], principal.token)
    return PaymentResponse(**row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8005)
