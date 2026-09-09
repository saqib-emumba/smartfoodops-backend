"""SmartFoodOps Orchestrator Service — the saga's health and telemetry surface (Port 8007).

Owns no database, and as of D47 owns no saga routes either. It once carried
`POST /api/v1/orchestrator/sagas` and `.../signals`, a pair of routes whose entire body was
`start_workflow` and `handle.signal` — an HTTP hop D36 introduced because starting a
workflow by class reference (`OrderWorkflow.run`) would have pulled the activities and their
payment/rider clients into the Order Service's process. Naming the workflow by *string*
closes that hole without the hop, so the services that observe saga facts now hold their own
Temporal client through `common.temporal.SagaClient` and talk to Temporal directly.

What remains here is the deployable's API side: a health probe reporting whether Temporal
answers, and the `/metrics` route `instrument_app` mounts. The real work of this service is
in the sibling process — `orchestrator/worker.py`, running whatever `registry.py` declares.

Adding a workflow does not touch this file. That is the point of the split: a new entity
needs `workflows/<entity>.py`, `activities/<entity>.py`, a task-queue constant, and one
registry line — never a route and never a schema.
"""

from fastapi import FastAPI

from common.responses import install_error_handlers
from common.telemetry import instrument_app
from orchestrator import deps
from orchestrator.apis import health

# One dependency, so no compose_lifespan — see common/lifespan.py's docstring for when
# that earns its keep (order, menu and user each hold two).
app = FastAPI(title="SmartFoodOps Orchestrator Service", lifespan=deps.temporal.lifespan)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

app.include_router(health.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8007)
