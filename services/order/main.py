"""SmartFoodOps Order Service — idempotent checkout (Port 8004).

Owns the `orders` table in its own PostgreSQL database — payments moved out to the Payment
Service (Port 8005) along with their table. Prices are always recalculated server-side from
the Menu Service's published menu, and audit logs are written through the Menu Service so
that this service never talks to MongoDB directly.

The customer and restaurant an order names live in other services' databases, so they are
verified over HTTP before the insert — see clients.py.
"""

from uuid import UUID

from fastapi import Depends, FastAPI, Header, Response, status

from clients import MenuServiceClient, RestaurantServiceClient, UserServiceClient
from common.auth import Principal, current_principal, require_role, require_self_or_admin
from common.config import required
from common.errors import bad_request, not_found
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from pricing import build_order_snapshot
from repository import OrderRepository
from schemas import OrderCreateRequest, OrderResponse

SERVICE_NAME = "order-service"
DATABASE_URL = required("DATABASE_URL")

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(
    DATABASE_URL,
    logger=logger,
    exhausted_detail="Database connection pool exhausted; order was not created",
)
orders = OrderRepository(db, logger=logger)
menu_service = MenuServiceClient(logger, service_name=SERVICE_NAME)
user_service = UserServiceClient(logger)
restaurant_service = RestaurantServiceClient(logger)

app = FastAPI(title="SmartFoodOps Order Service", lifespan=db.lifespan)


@app.get("/api/v1/orders/health")
def health():
    return {
        "status": "Orders Service operational",
        "service": SERVICE_NAME,
        "database_reachable": db.is_reachable(),
        "user_service_url": user_service.base_url,
        "restaurant_service_url": restaurant_service.base_url,
        "menu_service_url": menu_service.base_url,
    }


@app.post(
    "/api/v1/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_order(
    payload: OrderCreateRequest,
    response: Response,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
    principal: Principal = Depends(require_role("customer")),
) -> OrderResponse:
    """Create an order idempotently after re-pricing it against the live menu.

    The order is placed for the token's subject. There is no way to place one for anybody
    else — `customer_id` is not a field a client can send.
    """
    if not x_idempotency_key:
        raise bad_request("X-Idempotency-Key header is required")

    # (b) Replay protection — an already-seen key returns the stored order untouched.
    # Scoped to the caller: idempotency keys are client-chosen, so without this check a
    # guessed key would hand back somebody else's order.
    existing = orders.find_by_idempotency_key(x_idempotency_key)
    if existing is not None:
        require_self_or_admin(principal, existing["customer_id"])
        response.status_code = status.HTTP_200_OK
        logger.info("Idempotent replay for key %s", x_idempotency_key)
        return OrderResponse(**existing)

    # (c) Re-price from the Menu Service; unavailable items or a total mismatch abort here.
    menu = menu_service.fetch_menu(payload.restaurant_id, principal.token)
    items_snapshot, total = build_order_snapshot(menu, payload)

    # (d) Both participants live in other services' databases, so the foreign keys that
    # used to reject an unknown id at insert time are gone. The HTTP checks that replace
    # them sit here, immediately before the write, for the same reason. The customer check
    # also outlives the token's role claim: a demoted account fails here even while holding
    # a token minted before the change.
    user_service.verify_customer(principal.user_id, principal.token)
    restaurant_service.verify_restaurant(payload.restaurant_id, principal.token)

    order = orders.create(
        payload, principal.user_id, items_snapshot, total, x_idempotency_key
    )

    # (e) Audit trail is written through the Menu Service, never straight to MongoDB.
    menu_service.write_audit_log(order, x_idempotency_key)

    return OrderResponse(**order)


@app.get("/api/v1/orders/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: UUID,
    principal: Principal = Depends(current_principal),
) -> OrderResponse:
    """Expose an order — including its server-recalculated `total_amount`.

    Added for the Payment Service: `payments.order_id` used to be a foreign key into this
    database, and this endpoint is what replaced it. The total it returns is the figure a
    payment has to match, so the authoritative amount stays owned by this service.

    Readable by the customer who placed it, or an admin. The Payment Service reaches it
    while forwarding that customer's token, so paying for an order requires being the
    person who ordered it.
    """
    row = orders.find(order_id)
    if row is None:
        raise not_found(f"Order {order_id} not found")

    require_self_or_admin(principal, row["customer_id"])
    return OrderResponse(**row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
