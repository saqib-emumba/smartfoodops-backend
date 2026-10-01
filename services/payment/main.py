"""SmartFoodOps Payment Service — idempotent authorisation (Port 8005).

Owns the `payments` table in its own PostgreSQL database. It is a separate service because
card handling is the one part of the platform worth isolating on its own: the compliance
boundary shrinks to this container and its database, and a gateway outage can no longer
starve the threads that place, read and track orders.

The order a payment settles lives in the Order Service's database, so it is verified over
HTTP before the insert — see clients/order.py — and the amount is checked against the total that
service already recalculated from the live menu.

This module is the composition root: it builds the app and mounts the router. Singletons
live in deps.py, routes in apis/, and the one authorisation algorithm both charging routes
run in authorise.py.

Three lifespans as of D53 (was two, Week 3 through D39): `db.lifespan` for the connection
pool, `deps.kafka.lifespan` for the producer `apis/internal_events.py` publishes through
(replacing `outbox_relay.lifespan`), and `deps.temporal.lifespan` — new here — for the
client `apis/payments.py::process_payment` now starts `PaymentWorkflow` (manual mode, D55)
through.
"""

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from common.telemetry import instrument_app
from payment.apis import health, internal_events, payments, saga
from payment import deps

app = FastAPI(
    title="SmartFoodOps Payment Service",
    lifespan=compose_lifespan(deps.db.lifespan, deps.kafka.lifespan, deps.temporal.lifespan),
)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

app.include_router(health.router)
app.include_router(payments.router)
app.include_router(saga.router)
app.include_router(internal_events.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8005)
