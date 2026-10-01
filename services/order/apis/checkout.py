"""Checkout: place an order, then read it back — bearer and internal variants.

`create_order` is a thin wrapper as of D53/order-creation-temporal-update-design.md: it
derives the order's id, hands the raw cart to Temporal via Update-with-Start, and maps the
result onto 201/200. Re-pricing, verification and the insert itself moved to
apis/internal_orders.py, reached only through the workflow now.
"""

from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Header, Response, status

from common.auth import CurrentUser, get_current_user, require_internal, require_role, require_self_or_admin
from common.errors import not_found
from common.responses import Envelope, REPLAY_RESPONSE, ok
from common.temporal import ORDER_ID_NAMESPACE
from order import deps
from order.schemas.orders import OrderCreateRequest, OrderResponse

router = APIRouter(prefix="/api/v1/orders")


@router.post(
    "",
    response_model=Envelope[OrderResponse],
    status_code=status.HTTP_201_CREATED,
    responses=REPLAY_RESPONSE,
)
async def create_order(
    payload: OrderCreateRequest,
    response: Response,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("customer")),
) -> Envelope[OrderResponse]:
    """Place an order idempotently, via a Temporal Update-with-Start.

    `order_id` is derived deterministically from `(customer_id, idempotency_key)` — never
    the idempotency key alone, so two different customers reusing the same literal key
    string never address the same workflow (the database's own unique constraint on
    `idempotency_key` still independently catches that as a genuine `409`, raised by
    apis/internal_orders.py). This is what lets the workflow be addressed before Postgres
    has ever heard of the order.

    Temporal being unreachable now means no order can be created at all — see
    `common.temporal.SagaClient.start_with_update`, which raises `503` rather than
    degrading gracefully the way D25's fire-and-forget saga start used to.
    """
    order_id = uuid5(ORDER_ID_NAMESPACE, f"{current_user.user_id}:{x_idempotency_key}")

    result = await deps.orchestrator_service.create_order(
        order_id,
        payload,
        current_user=current_user,
        idempotency_key=x_idempotency_key,
    )
    if not result["created"]:
        require_self_or_admin(current_user, result["order"]["customer_id"])
        response.status_code = status.HTTP_200_OK
        return ok(
            OrderResponse(**result["order"]), message="This order has already been placed", status=200
        )
    return ok(OrderResponse(**result["order"]), message="Order placed", status=201)


@router.get("/{order_id}", response_model=Envelope[OrderResponse])
def get_order(
    order_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> Envelope[OrderResponse]:
    """Expose an order — including its server-recalculated `total_amount`.

    Added for the Payment Service: `payments.order_id` used to be a foreign key into this
    database, and this endpoint is what replaced it. The total it returns is the figure a
    payment has to match, so the authoritative amount stays owned by this service.

    Readable by the customer who placed it, or an admin. The Payment Service reaches it
    while asserting that customer's own identity, so paying for an order requires being the
    person who ordered it.
    """
    row = deps.orders.find(order_id)
    if row is None:
        raise not_found(f"Order {order_id} not found")

    require_self_or_admin(current_user, row["customer_id"])
    return ok(OrderResponse(**row), message="Order found")


@router.get(
    "/{order_id}/internal",
    response_model=Envelope[OrderResponse],
    dependencies=[Depends(require_internal)],
)
def get_order_internally(order_id: UUID) -> Envelope[OrderResponse]:
    """The same order as the endpoint above, for callers with no user behind them.

    The Payment Service's saga path needs the authoritative `total_amount` but holds no
    bearer token to forward — a workflow is not a user (D26). The ownership check the
    bearer version performs is not lost, only relocated: the saga did not choose this
    order, it was started by an already-authorised `POST /api/v1/orders` whose handler had
    established that the caller owns it.

    Internal-key only, because without the token there is no ownership check left here, so
    this must not be reachable by anyone who could guess an order id.
    """
    row = deps.orders.find(order_id)
    if row is None:
        raise not_found(f"Order {order_id} not found")
    return ok(OrderResponse(**row), message="Order found")
