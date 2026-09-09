"""SmartFoodOps User Service — the platform's identity provider (Port 8001).

Owns the `users` table and the `roles` lookup beside it in its own PostgreSQL database, and
is the only service holding the RS256 private key: every other service verifies tokens and
none can mint them.

Refresh sessions live in Redis logical database 1, kept clear of the Menu Service's cache in
database 0 — see repositories/sessions.py.

This module is the composition root: it builds the app, composes the two lifespans, and
mounts the routers. Singletons live in deps.py, routes in apis/, and the password/session
mechanics in security.py.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from common.telemetry import instrument_app
from user.apis import health, sessions, users
from user import deps


@asynccontextmanager
async def _refresh_store_lifespan(_: FastAPI):
    """Hold the refresh store open for the whole process."""
    deps.refresh_tokens.connect()
    try:
        yield
    finally:
        deps.refresh_tokens.close()


app = FastAPI(
    title="SmartFoodOps User Service",
    lifespan=compose_lifespan(deps.db.lifespan, _refresh_store_lifespan),
)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

app.include_router(health.router)
app.include_router(users.router)
app.include_router(sessions.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
