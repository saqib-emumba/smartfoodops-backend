"""SmartFoodOps Analytics Service — a Kafka read-model (Port 8008).

This module is the composition root: it builds the app, composes the two lifespans, and
mounts the router. Singletons live in deps.py, the consumer in consumer.py.

Two lifespans: `db.lifespan` for the connection pool `order_projections` and
`processed_events` live in, and `deps.consumer.lifespan` (Week 3, D40) for the background
Kafka consumer — the same `compose_lifespan` shape order-service and payment-service use
for their outbox relays, mirrored here for the read side.
"""

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from common.telemetry import instrument_app
from analytics import deps
from analytics.apis import health

app = FastAPI(
    title="SmartFoodOps Analytics Service",
    lifespan=compose_lifespan(deps.db.lifespan, deps.consumer.lifespan),
)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

app.include_router(health.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8008)
