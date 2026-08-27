"""Checkout: place an order, then read it back — bearer and internal variants."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status

from common.auth import CurrentUser, get_current_user, require_internal, require_role, require_self_or_admin
from common.errors import not_found
from order import deps
from order.pricing import build_order_snapshot
from order.saga import start_saga
from order.schemas.orders import OrderCreateRequest, OrderResponse

router = APIRouter(prefix="/api/v1/orders")


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreateRequest,
    response: Response,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("customer")),
) -> OrderResponse:
    """Create an order idempotently after re-pricing it against the live menu.

    The order is placed for the token's subject. There is no way to place one for anybody
    else — `customer_id` is not a field a client can send.

    Since Week 2 this also hands the committed order to the saga, which is what carries it
    from `created` to `delivered`. Everything before that step is unchanged.
    """
    # (b) Replay protection — an already-seen key returns the stored order untouched.
    # Scoped to the caller: idempotency keys are client-chosen, so without this check a
    # guessed key would hand back somebody else's order.
    existing = deps.orders.find_by_idempotency_key(x_idempotency_key)
    if existing is not None:
        require_self_or_admin(current_user, existing["customer_id"])
        response.status_code = status.HTTP_200_OK
        deps.logger.info("Idempotent replay for key %s", x_idempotency_key)
        # A replay also re-attempts the saga. This is what repairs an order whose workflow
        # failed to start the first time: the workflow id is derived from the order id, so
        # a saga that is already running is left alone, and one that never began now does.
        # The restaurant is re-read because a replay has none in hand — the cost of making
        # this path self-healing, paid only on an actual retry.
        await start_saga(
            existing,
            deps.restaurant_service.verify_restaurant(
                existing["restaurant_id"], current_user.token
            ),
        )
        return OrderResponse(**existing)

    # (c) Re-price from the Menu Service; unavailable items or a total mismatch abort here.
    menu = deps.menu_service.fetch_menu(payload.restaurant_id, current_user.token)
    items_snapshot, total = build_order_snapshot(menu, payload)

    # (d) Both participants live in other services' databases, so the foreign keys that
    # used to reject an unknown id at insert time are gone. The HTTP checks that replace
    # them sit here, immediately before the write, for the same reason. The customer check
    # also outlives the token's role claim: a demoted account fails here even while holding
    # a token minted before the change.
    deps.user_service.verify_customer(current_user.user_id, current_user.token)
    # The response is kept, not discarded: `capacity`, `latitude` and `longitude` are on it,
    # and handing them to the saga in its payload is what removed the saga's four HTTP calls
    # to the Restaurant Service (D32). Captured here, at checkout, from a lookup that was
    # already happening.
    restaurant = deps.restaurant_service.verify_restaurant(
        payload.restaurant_id, current_user.token
    )

    # (e) The order and the opening 'created' entry of its audit trail commit together —
    # same database, one transaction. There is no window in which one exists without the
    # other, which is what the cross-service HTTP log call could never promise.
    order = deps.orders.create(
        payload, current_user.user_id, items_snapshot, total, x_idempotency_key
    )

    # (f) The order exists; the saga runs it from here.
    await start_saga(order, restaurant)

    return OrderResponse(**order)


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> OrderResponse:
    """Expose an order — including its server-recalculated `total_amount`.

    Added for the Payment Service: `payments.order_id` used to be a foreign key into this
    database, and this endpoint is what replaced it. The total it returns is the figure a
    payment has to match, so the authoritative amount stays owned by this service.

    Readable by the customer who placed it, or an admin. The Payment Service reaches it
    while forwarding that customer's token, so paying for an order requires being the
    person who ordered it.
    """
    row = deps.orders.find(order_id)
    if row is None:
        raise not_found(f"Order {order_id} not found")

    require_self_or_admin(current_user, row["customer_id"])
    return OrderResponse(**row)


@router.get(
    "/{order_id}/internal",
    response_model=OrderResponse,
    dependencies=[Depends(require_internal)],
)
def get_order_internally(order_id: UUID) -> OrderResponse:
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
    return OrderResponse(**row)
