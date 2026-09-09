"""SmartFoodOps Orchestrator Service — the order saga's Temporal front door (Port 8007).

Owns no database. Split out of the Order Service (D36): before this, `order-service` held
its own Temporal client to start workflows and relay signals, and `order-worker` — sharing
its image and database — executed them. Now this service's API side does the former and
`orchestrator/worker.py` the latter, and neither touches `sfo_order_core`; every fact about
an order's state is reached over HTTP, through `orchestrator/clients/order/order_service.py`.

Internal-key only, end to end. No end user, and no sibling but the Order Service, ever
calls this service directly — see `apis/order.py`'s router-level guard.

This module is the composition root: it builds the app, holds the Temporal lifespan, and
mounts the routers. Singletons live in deps.py, routes in apis/ — one router per entity
this service orchestrates, plus `health`. A future second entity's routes mount here the
same way `order.router` does.
"""

from fastapi import FastAPI

from common.responses import install_error_handlers
from common.telemetry import instrument_app
from orchestrator import deps
from orchestrator.apis import health, order

# One dependency, so no compose_lifespan — see common/lifespan.py's docstring for when
# that earns its keep (order, menu and user each hold two).
app = FastAPI(title="SmartFoodOps Orchestrator Service", lifespan=deps.temporal.lifespan)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

app.include_router(health.router)
app.include_router(order.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8007)
