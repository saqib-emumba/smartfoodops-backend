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
"""

from fastapi import FastAPI

from payment.apis import payments, saga
from payment import deps

app = FastAPI(title="SmartFoodOps Payment Service", lifespan=deps.db.lifespan)

app.include_router(payments.router)
app.include_router(saga.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8005)
