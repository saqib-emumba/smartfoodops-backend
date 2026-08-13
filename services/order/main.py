"""SmartFoodOps Order Service — idempotent checkout (Port 8004).

Owns the `orders` and `payments` tables in its own PostgreSQL database. Prices are always
recalculated server-side from the Menu Service's published menu, and audit logs are written
through the Menu Service so that this service never talks to MongoDB directly.

The customer and restaurant an order names live in other services' databases, so they are
verified over HTTP before the insert — see clients.py.
"""

from fastapi import FastAPI, Header, Response, status

from clients import MenuServiceClient, RestaurantServiceClient, UserServiceClient
from common.config import required
from common.errors import bad_request
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
) -> OrderResponse:
    """Create an order idempotently after re-pricing it against the live menu."""
    if not x_idempotency_key:
        raise bad_request("X-Idempotency-Key header is required")

    # (b) Replay protection — an already-seen key returns the stored order untouched.
    existing = orders.find_by_idempotency_key(x_idempotency_key)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        logger.info("Idempotent replay for key %s", x_idempotency_key)
        return OrderResponse(**existing)

    # (c) Re-price from the Menu Service; unavailable items or a total mismatch abort here.
    menu = menu_service.fetch_menu(payload.restaurant_id)
    items_snapshot, total = build_order_snapshot(menu, payload)

    # (d) Both participants live in other services' databases, so the foreign keys that
    # used to reject an unknown id at insert time are gone. The HTTP checks that replace
    # them sit here, immediately before the write, for the same reason.
    user_service.verify_customer(payload.customer_id)
    restaurant_service.verify_restaurant(payload.restaurant_id)

    order = orders.create(payload, items_snapshot, total, x_idempotency_key)

    # (e) Audit trail is written through the Menu Service, never straight to MongoDB.
    menu_service.write_audit_log(order, x_idempotency_key)

    return OrderResponse(**order)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
