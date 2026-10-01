"""SmartFoodOps Order Service — idempotent checkout (Port 8004).

Owns the `orders` table in its own PostgreSQL database, and the `order_tracking_logs`
trail beside it — payments moved out to the Payment Service (Port 8005) along with their
table, while the trail moved in from the Menu Service's MongoDB, because which state an
order is in is this service's fact. Prices are always recalculated server-side from the
Menu Service's published menu.

The customer and restaurant an order names live in other services' databases, so they are
verified over HTTP before the insert — see clients/.

This module is the composition root: it builds the app and mounts the routers. Singletons
live in deps.py, the saga hand-off in clients/orchestrator.py, and the routes are grouped by
domain area in apis/.

Three lifespans. D36 took this down to one; Week 3's outbox relay made it two; D47 brought
the Temporal client back as the third. D53 keeps it at three but swaps what the second one
is: `db.lifespan` holds the connection pool, `deps.kafka.lifespan` connects the producer
`apis/internal_events.py` publishes through (replacing `outbox_relay.lifespan`), and
`deps.temporal.lifespan` connects the client this service creates orders and records kitchen
decisions through — log-and-swallow on failure, because reads must stay up when Temporal is
down even though, since D53, writes cannot.
"""

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from common.telemetry import instrument_app
from order import deps
from order.apis import (
    checkout,
    health,
    internal_events,
    internal_kitchen,
    internal_orders,
    kitchen,
    rider_reports,
    tracking,
    transitions,
)

app = FastAPI(
    title="SmartFoodOps Order Service",
    lifespan=compose_lifespan(deps.db.lifespan, deps.kafka.lifespan, deps.temporal.lifespan),
)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

# health first: it and checkout.router's GET /{order_id} are both single-segment GET paths
# under this prefix, and Starlette matches by registration order — a parameterised route
# registered first would swallow /health and answer 422 trying to parse "health" as a UUID.
app.include_router(health.router)
app.include_router(checkout.router)
app.include_router(kitchen.router)
app.include_router(rider_reports.router)
app.include_router(tracking.router)
app.include_router(transitions.router)
app.include_router(internal_orders.router)
app.include_router(internal_events.router)
app.include_router(internal_kitchen.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
