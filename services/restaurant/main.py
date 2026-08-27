"""SmartFoodOps Restaurant Service — onboarding and lookups (Port 8002).

Owns the `restaurants` table in its own PostgreSQL database. Owner identity/authorisation is
resolved over HTTP against the User Service, whose database this service cannot reach.

This service briefly owned the kitchen queue as well. D32 moved it: a kitchen's decision is
a fact about an *order*, and every other fact about an order's lifecycle already lived in the
Order Service's database, so `order_tickets` was a cross-database hop earning nothing. What
stays here is the thing that genuinely is restaurant-domain — `capacity`, which the Order
Service reads at checkout and hands to the saga.

Whether a caller owns a restaurant remains this service's fact, and the Order Service
resolves it here over HTTP before letting an admin decide an order (D16).

This module is the composition root: it builds the app and mounts the routers. The
singletons live in deps.py and the routes in apis/ — see deps.py for why that split exists.
"""

from fastapi import FastAPI

from restaurant.apis import restaurants
from restaurant import deps

app = FastAPI(title="SmartFoodOps Restaurant Service", lifespan=deps.db.lifespan)

app.include_router(restaurants.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
