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

Authorisation for a pickup or delivery is settled entirely here, from `current_order_id` on
the rider's own row, rather than by asking the Order Service who was assigned. One place
decides, so there is no second place to drift (D16).

This module is the composition root: it builds the app and mounts the three routers.
Singletons live in deps.py; the routes are grouped by who may call them in api/.
"""

import os

from fastapi import FastAPI

from common.responses import install_error_handlers
from common.telemetry import instrument_app
from rider import deps
from rider.apis import delivery, dispatch, health, profile

app = FastAPI(title="SmartFoodOps Rider Service", lifespan=deps.db.lifespan)
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
