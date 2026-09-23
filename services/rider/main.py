"""SmartFoodOps Rider Service — the delivery fleet and proximity dispatch (Port 8006).

Owns the `riders` table in its own PostgreSQL database. That table shipped inside
`sfo_user_core` in Week 1 with no code behind it; Week 2 gave it a service that writes
availability and location on every assignment, and under D01 a service may not write another
service's tables — so it moved here, and its foreign key to `users` became a plain UUID
verified over HTTP (D02, D28).

Two kinds of caller reach this service. Riders themselves, on a bearer token, manage their
own profile and report pickups and deliveries. The order saga, on the internal key, claims
and releases them. No end user may reach `/dispatch` or `/release`: which rider carries which
order is the fleet's business, not a customer's.

This service knows Temporal exists as of D47. A reported pickup or delivery is recorded
against the order through the Order Service and then signalled into the saga from here —
previously both halves were one relay call and the Order Service did the signalling. See
apis/delivery.py for why the write still crosses a boundary and the signal does not.

Authorisation for a pickup or delivery is settled entirely here, from `current_order_id` on
the rider's own row, rather than by asking the Order Service who was assigned. One place
decides, so there is no second place to drift (D16).

This module is the composition root: it builds the app and mounts the three routers.
Singletons live in deps.py; the routes are grouped by who may call them in api/.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from common.telemetry import instrument_app
from rider import deps
from rider.apis import delivery, dispatch, health, profile


@asynccontextmanager
async def _geo_lifespan(_: FastAPI):
    """Hold the fleet's Redis geo-index connection open for the whole process (D49)."""
    deps.geo.connect()
    try:
        yield
    finally:
        deps.geo.close()


# Three lifespans as of D49: the connection pool, the Temporal client this service signals
# the saga with (D47), and the Redis geo-index rider location now lives in. The gateway's
# own lifespan log-and-swallows a startup failure, so a rider can still register and move
# while Temporal is down — what fails then is the signal, and the saga's timeout read-back
# of `orders.rider_reported_stage` is what repairs that. Redis has no such fallback for
# dispatch specifically (see repositories/geo.py) — a location ping or a dispatch attempt
# genuinely fails while it's down.
app = FastAPI(
    title="SmartFoodOps Rider Service",
    lifespan=compose_lifespan(deps.db.lifespan, deps.temporal.lifespan, _geo_lifespan),
)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

# `health` first: a literal segment must be registered before any parameterised sibling
# that could shadow it, and Starlette matches in registration order.
app.include_router(health.router)
app.include_router(profile.router)
app.include_router(delivery.router)
app.include_router(dispatch.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8006")))
