"""SmartFoodOps Menu Service — hierarchical menus, read through a cache (Port 8003).

Owns the `menus` table in its own PostgreSQL database, with Redis in front of it as a
cache-aside layer. Both replaced MongoDB: the category tree survived the move whole,
inside a JSONB column — see readme/postgres-menu-tracking-migration-v2.md.

`order_tracking_logs` left with the same migration, in the other direction: it now lives
beside `orders` in the Order Service's database, where a status transition can be a real
foreign key against the order it describes. This service no longer takes audit writes.

Restaurant existence and active state live in the Restaurant Service's database, so they
are resolved over HTTP rather than joined.
"""

import os
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI
from pydantic import ValidationError

from cache import MenuCache
from clients import RestaurantServiceClient
from common.auth import Principal, current_principal, require_role
from common.config import DEFAULT_REDIS_URL, required
from common.errors import forbidden, not_found
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from repository import MenuRepository
from schemas import MenuResponse, MenuUpsertRequest

SERVICE_NAME = "menu-service"
DATABASE_URL = required("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(
    DATABASE_URL,
    logger=logger,
    exhausted_detail="Database connection pool exhausted; the menu could not be served",
)
menus = MenuRepository(db)
cache = MenuCache(REDIS_URL, logger=logger)
restaurant_service = RestaurantServiceClient(logger)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hold the connection pool and the menu cache open for the whole process."""
    async with db.lifespan(app):
        cache.connect()
        try:
            yield
        finally:
            cache.close()


app = FastAPI(title="SmartFoodOps Menu Service", lifespan=lifespan)


def _as_response(row: dict) -> MenuResponse:
    return MenuResponse(
        restaurant_id=row["restaurant_id"], categories=row["categories"]
    )


@app.get("/api/v1/menus/health")
def health():
    return {
        "status": "Menu Service running with PostgreSQL + Redis cache",
        "service": SERVICE_NAME,
        "database_reachable": db.is_reachable(),
        "cache_reachable": cache.is_reachable(),
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

    Async only because the ownership check is an outbound HTTP call; the database work
    below is blocking and brief, which is the same trade the write path always made.
    """
    restaurant = await restaurant_service.verify_active(
        payload.restaurant_id, principal.token
    )
    if str(restaurant.get("owner_id")) != str(principal.user_id) and not principal.is_admin:
        raise forbidden(f"You do not own restaurant {payload.restaurant_id}")

    categories = [category.model_dump() for category in payload.categories]
    row = menus.upsert(payload.restaurant_id, categories)

    # Invalidate only after the row is committed. Dropping the key first would let a
    # concurrent reader repopulate the cache from the old row and leave the stale copy
    # behind the write that was supposed to replace it.
    cache.invalidate(payload.restaurant_id)
    return _as_response(row)


@app.get("/api/v1/menus/{restaurant_id}", response_model=MenuResponse)
def get_menu(
    restaurant_id: UUID,
    _: Principal = Depends(current_principal),
) -> MenuResponse:
    """Serve the active menu tree — used by the Order Service to price a checkout.

    Cache-aside: Redis, then Postgres, then populate. Every checkout reads this endpoint,
    so the hot path is a single key lookup; a cache miss or a Redis outage costs one query
    rather than an error, because the cache is a copy and never the source of truth.

    Any authenticated caller: customers browse it, and the Order Service reads it while
    forwarding the customer's own token.
    """
    cached = cache.get(restaurant_id)
    if cached is not None:
        try:
            return MenuResponse.model_validate_json(cached)
        except ValidationError as exc:
            # A payload written by an older build of this service. Treat it as a miss and
            # let the read below overwrite it rather than failing a request over it.
            logger.warning("Discarding unreadable cached menu %s: %s", restaurant_id, exc)

    row = menus.find(restaurant_id)
    if row is None:
        raise not_found(f"No menu published for restaurant {restaurant_id}")

    menu = _as_response(row)
    cache.store(restaurant_id, menu.model_dump_json())
    return menu


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
