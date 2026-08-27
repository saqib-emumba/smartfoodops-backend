"""SmartFoodOps Order Service — idempotent checkout (Port 8004).

Owns the `orders` table in its own PostgreSQL database, and the `order_tracking_logs`
trail beside it — payments moved out to the Payment Service (Port 8005) along with their
table, while the trail moved in from the Menu Service's MongoDB, because which state an
order is in is this service's fact. Prices are always recalculated server-side from the
Menu Service's published menu.

The customer and restaurant an order names live in other services' databases, so they are
verified over HTTP before the insert — see clients/.

This module is the composition root: it builds the app and mounts the six routers.
Singletons live in deps.py, the saga hand-off in clients/orchestrator.py, and the routes
are grouped by domain area in apis/.

One lifespan now, not two (D36): the Temporal client this service used to hold directly —
alongside `db.lifespan` — moved with the worker to the Orchestrator Service, so there is
only `PostgresPool`'s left to compose.
"""

from fastapi import FastAPI

from common.responses import install_error_handlers
from order import deps
from order.apis import checkout, health, kitchen, signals, tracking, transitions

app = FastAPI(title="SmartFoodOps Order Service", lifespan=deps.db.lifespan)
install_error_handlers(app)

# health first: it and checkout.router's GET /{order_id} are both single-segment GET paths
# under this prefix, and Starlette matches by registration order — a parameterised route
# registered first would swallow /health and answer 422 trying to parse "health" as a UUID.
app.include_router(health.router)
app.include_router(checkout.router)
app.include_router(kitchen.router)
app.include_router(signals.router)
app.include_router(tracking.router)
app.include_router(transitions.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
