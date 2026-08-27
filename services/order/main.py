"""SmartFoodOps Order Service — idempotent checkout (Port 8004).

Owns the `orders` table in its own PostgreSQL database, and the `order_tracking_logs`
trail beside it — payments moved out to the Payment Service (Port 8005) along with their
table, while the trail moved in from the Menu Service's MongoDB, because which state an
order is in is this service's fact. Prices are always recalculated server-side from the
Menu Service's published menu.

The customer and restaurant an order names live in other services' databases, so they are
verified over HTTP before the insert — see clients/.

This module is the composition root: it builds the app, composes the two lifespans, and
mounts the four routers. Singletons live in deps.py, the saga hand-off in saga.py, and the
routes are grouped by domain area in api/.
"""

from fastapi import FastAPI

from common.health import health_payload
from common.lifespan import compose_lifespan
from order import deps
from order.apis import checkout, kitchen, signals, tracking

# FastAPI takes a single lifespan and this service has two dependencies that need one.
# compose_lifespan enters them in order and exits in reverse, so neither has to know about
# the other and `PostgresPool` stays reusable by the services that need no orchestrator.
app = FastAPI(
    title="SmartFoodOps Order Service",
    lifespan=compose_lifespan(deps.db.lifespan, deps.temporal.lifespan),
)


@app.get("/api/v1/orders/health")
async def health():
    return health_payload(
        deps.SERVICE_NAME,
        deps.db,
        status="Orders Service operational",
        # Unlike `database_reachable`, this being false does not mean the service is
        # degraded for reads: orders can still be placed and fetched. What stops is the
        # saga advancing them, which a retry repairs once the orchestrator returns.
        temporal_reachable=await deps.temporal.is_reachable(),
        temporal_address=deps.temporal.address,
        user_service_url=deps.user_service.base_url,
        restaurant_service_url=deps.restaurant_service.base_url,
        menu_service_url=deps.menu_service.base_url,
    )


# health is declared directly above, before any router is included, because it and
# checkout.router's GET /{order_id} are both single-segment GET paths under this prefix —
# Starlette matches by registration order, and a parameterised route registered first would
# swallow /health and answer 422 trying to parse "health" as a UUID.
app.include_router(checkout.router)
app.include_router(kitchen.router)
app.include_router(signals.router)
app.include_router(tracking.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
