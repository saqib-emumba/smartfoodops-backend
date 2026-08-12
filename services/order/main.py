"""SmartFoodOps Order Service — idempotent checkout (Port 8004).

Owns the PostgreSQL `orders` table. Prices are always recalculated server-side from the
Menu Service's published menu, and audit logs are written through the Menu Service so that
this service never talks to MongoDB directly.
"""

import os

from fastapi import FastAPI, Header, Response, status

from clients import MenuServiceClient
from common.config import DEFAULT_DATABASE_URL
from common.errors import bad_request
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from pricing import build_order_snapshot
from repository import OrderRepository
from schemas import OrderCreateRequest, OrderResponse

SERVICE_NAME = "order-service"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(
    DATABASE_URL,
    logger=logger,
    exhausted_detail="Database connection pool exhausted; order was not created",
)
orders = OrderRepository(db, logger=logger)
menu_service = MenuServiceClient(logger, service_name=SERVICE_NAME)

app = FastAPI(title="SmartFoodOps Order Service", lifespan=db.lifespan)


@app.get("/api/v1/orders/health")
def health():
    return {
        "status": "Orders Service operational",
        "service": SERVICE_NAME,
        "database_reachable": db.is_reachable(),
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

    order = orders.create(payload, items_snapshot, total, x_idempotency_key)

    # (d) Audit trail is written through the Menu Service, never straight to MongoDB.
    menu_service.write_audit_log(order, x_idempotency_key)

    return OrderResponse(**order)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
