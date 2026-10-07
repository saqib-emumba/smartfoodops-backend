"""SmartFoodOps User Service — the platform's identity provider (Port 8001).

Owns the `users` table and the `roles` lookup beside it in its own PostgreSQL database, and
is the only service holding the RS256 private key: every other service verifies tokens and
none can mint them.

Refresh sessions live in Redis logical database 1, kept clear of the Menu Service's cache in
database 0 — see repositories/sessions.py.

Since D57 it also owns the platform's authorization policy — the `permissions`,
`role_permissions` and `route_permissions` tables — and answers the gateway's per-request
"may this caller do this" question from apis/internal.py. That makes it the one service whose
availability every gated request depends on, which is why the policy is held in memory
(policy.py) rather than read from Postgres per call.

This module is the composition root: it builds the app, composes the three lifespans, and
mounts the routers. Singletons live in deps.py, routes in apis/, the policy decision point in
policy.py, and the password/session mechanics in security.py.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from common.telemetry import instrument_app
from user.apis import health, internal, roles, sessions, users
from user import deps, policy


@asynccontextmanager
async def _refresh_store_lifespan(_: FastAPI):
    """Hold the refresh store open for the whole process."""
    deps.refresh_tokens.connect()
    try:
        yield
    finally:
        deps.refresh_tokens.close()


@asynccontextmanager
async def _policy_lifespan(_: FastAPI):
    """Load the authorization policy before the service accepts traffic (D57).

    Must be composed *after* `deps.db.lifespan`: `PostgresPool.cursor()` raises until that
    lifespan has opened the pool, so the ordering below is load-bearing rather than
    cosmetic. `load_initial` raises on an empty policy, which fails startup on purpose —
    see services/user/policy.py.
    """
    policy.load_initial()
    yield


app = FastAPI(
    title="SmartFoodOps User Service",
    lifespan=compose_lifespan(deps.db.lifespan, _refresh_store_lifespan, _policy_lifespan),
)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

app.include_router(health.router)
app.include_router(internal.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(sessions.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
