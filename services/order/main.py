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

D36 took this down to one lifespan; Week 3 brings it back to two, for a different
reason. `db.lifespan` still holds the connection pool; `deps.outbox_relay.lifespan` (D39)
runs the background relay that publishes `order_outbox` rows to Kafka — genuinely a second,
independent concern this time, not the Temporal client D36 removed.
"""

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from common.telemetry import instrument_app
from order import deps
from order.apis import checkout, health, kitchen, signals, tracking, transitions

# Two lifespans again as of Week 3, having been one since D36: the outbox relay
# (deps.outbox_relay, D39) runs alongside the connection pool for this process' whole
# life, publishing order_outbox rows to Kafka in the background.
app = FastAPI(
    title="SmartFoodOps Order Service",
    lifespan=compose_lifespan(deps.db.lifespan, deps.outbox_relay.lifespan),
)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

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
