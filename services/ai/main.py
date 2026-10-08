"""SmartFoodOps AI Service — semantic retrieval (Port 8009).

The composition root: it builds the app, opens the pool, and mounts the routers. Singletons
live in deps.py, SQL in repositories/, and the request handlers in apis/.

The embedding model is loaded by deps.py at import; the vector database is only touched through
`deps.db`, whose pool this lifespan opens. Ingestion (keeping the vectors in step with the menus)
is a separate process — `ai.ingestion` — so a slow embedding job can never stall a search.
"""

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from common.telemetry import instrument_app
from ai import deps
from ai.apis import health

app = FastAPI(
    title="SmartFoodOps AI Service",
    lifespan=compose_lifespan(deps.db.lifespan),
)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

app.include_router(health.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8009)
