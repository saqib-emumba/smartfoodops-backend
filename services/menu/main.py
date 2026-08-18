"""SmartFoodOps Menu Service — hierarchical menus and order audit logs (Port 8003).

Owns the MongoDB `menus` and `order_tracking_logs` collections. Restaurant existence and
active state are resolved over HTTP against the Restaurant Service; this service never
touches PostgreSQL.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, FastAPI, status

from clients import RestaurantServiceClient
from common.auth import Principal, current_principal, require_internal, require_role
from common.errors import forbidden, not_found
from common.logging_config import configure_logging
from datastores import DocumentStores
from repository import MenuRepository
from schemas import MenuResponse, MenuUpsertRequest, OrderTrackingLogCreateRequest

SERVICE_NAME = "menu-service"

logger = configure_logging(SERVICE_NAME)
stores = DocumentStores(logger=logger)
menus = MenuRepository(stores)
restaurant_service = RestaurantServiceClient(logger)

app = FastAPI(title="SmartFoodOps Menu Service", lifespan=stores.lifespan)


def _as_response(document: dict) -> MenuResponse:
    return MenuResponse(
        restaurant_id=document["restaurant_id"], categories=document["categories"]
    )


@app.get("/api/v1/menus/health")
async def health():
    return {
        "status": "Menu Service running with NoSQL + Redis connection",
        "service": SERVICE_NAME,
        "mongo_reachable": await stores.mongo_reachable(),
        "redis_reachable": await stores.redis_reachable(),
        "restaurant_service_url": restaurant_service.base_url,
    }


@app.post("/api/v1/menus", response_model=MenuResponse)
async def upsert_menu(
    payload: MenuUpsertRequest,
    principal: Principal = Depends(require_role("restaurant_admin")),
) -> MenuResponse:
    """Upsert the full category/item/customization tree for one restaurant.

    Holding the `restaurant_admin` role is not enough — the caller must own *this*
    restaurant, or any restaurant admin could rewrite a competitor's prices.
    """
    restaurant = await restaurant_service.verify_active(
        payload.restaurant_id, principal.token
    )
    if str(restaurant.get("owner_id")) != str(principal.user_id) and not principal.is_admin:
        raise forbidden(f"You do not own restaurant {payload.restaurant_id}")

    categories = [category.model_dump() for category in payload.categories]
    document = await menus.upsert_menu(payload.restaurant_id, categories)
    return _as_response(document)


@app.post(
    "/api/v1/menus/logs",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal)],
)
async def log_order_status(payload: OrderTrackingLogCreateRequest):
    """Append a status transition to the order's `order_tracking_logs` document.

    Service-to-service only, on the internal key rather than a bearer token. The Order
    Service calls this on a customer's behalf, but the customer must not be able to call
    it themselves: the audit trail cannot be writable by the party it is about.
    """
    entry = {
        "status": payload.status,
        "timestamp": datetime.now(timezone.utc),
        "service": payload.service,
        "raw_log": payload.raw_log,
        "updated_by": payload.updated_by,
        "metadata": payload.metadata or {},
    }
    created = await menus.append_status_log(payload.order_id, entry)

    return {
        "message": "Audit log captured",
        "order_id": str(payload.order_id),
        "created_document": created,
        "status": payload.status,
    }


@app.get("/api/v1/menus/{restaurant_id}", response_model=MenuResponse)
async def get_menu(
    restaurant_id: UUID,
    _: Principal = Depends(current_principal),
) -> MenuResponse:
    """Serve the active menu tree — used by the Order Service to price a checkout.

    Any authenticated caller: customers browse it, and the Order Service reads it while
    forwarding the customer's own token.
    """
    document = await menus.find_menu(restaurant_id)
    if document is None:
        raise not_found(f"No menu published for restaurant {restaurant_id}")
    return _as_response(document)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
