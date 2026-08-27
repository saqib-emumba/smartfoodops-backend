"""SmartFoodOps Menu Service — hierarchical menus, read through a cache (Port 8003).

Owns the `menus` table in its own PostgreSQL database, with Redis in front of it as a
cache-aside layer. Both replaced MongoDB: the category tree survived the move whole,
inside a JSONB column — see readme/postgres-menu-tracking-migration-v2.md.

`order_tracking_logs` left with the same migration, in the other direction: it now lives
beside `orders` in the Order Service's database, where a status transition can be a real
foreign key against the order it describes. This service no longer takes audit writes.

Restaurant existence and active state live in the Restaurant Service's database, so they
are resolved over HTTP rather than joined.

This module is the composition root: it builds the app, composes the two lifespans, and
mounts the router. Singletons live in deps.py and routes in apis/.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from menu.apis import health, menus
from menu import deps


@asynccontextmanager
async def _cache_lifespan(_: FastAPI):
    """Hold the menu cache open for the whole process."""
    deps.cache.connect()
    try:
        yield
    finally:
        deps.cache.close()


app = FastAPI(
    title="SmartFoodOps Menu Service",
    lifespan=compose_lifespan(deps.db.lifespan, _cache_lifespan),
)
install_error_handlers(app)

app.include_router(health.router)
app.include_router(menus.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
