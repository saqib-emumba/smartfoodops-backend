"""SmartFoodOps AI Service — semantic retrieval (Port 8009).

The composition root: it builds the app, opens the pool, and mounts the routers. Singletons
live in deps.py, SQL in repositories/, and the request handlers in apis/.

The embedding model is loaded by deps.py at import; the vector database is only touched through
`deps.db`, whose pool the first lifespan opens and the second registers pgvector's types on. Ingestion (keeping the vectors in step with the menus)
is a separate process — `ai.ingestion` — so a slow embedding job can never stall a search.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.lifespan import compose_lifespan
from common.responses import install_error_handlers
from common.telemetry import instrument_app
from ai import config, deps
from ai import assistant_deps
from ai.apis import assistant, explain, health, partner, rag, search

@asynccontextmanager
async def _vector_lifespan(_: FastAPI):
    """Register pgvector's types once the pool is open. Composed *after* `db.lifespan`, which
    must have opened the pool before a connection can be leased to look the type up."""
    deps.register_vector_types()
    yield


def _warn_if_ollama_model_missing() -> None:
    """Say so at startup if `OLLAMA_MODEL` is not among the models Ollama has pulled.

    Without this the first question fails with a 503 and the reason (a 404 from Ollama) is only in the log,
    which is a slow way to learn that `OLLAMA_MODEL` names a model nobody pulled. Best-effort: an Ollama that
    is simply not running yet is reported, not fatal, since it may start after this service does.
    """
    import httpx

    try:
        tags = httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=3.0).json().get("models", [])
    except Exception as exc:  # noqa: BLE001
        deps.logger.warning("Ollama is not reachable at %s (%s); the assistant will answer 503 until it is", config.OLLAMA_BASE_URL, type(exc).__name__)
        return
    pulled = sorted(m.get("name", "") for m in tags)
    if config.OLLAMA_MODEL not in pulled and f"{config.OLLAMA_MODEL}:latest" not in pulled:
        deps.logger.warning(
            "OLLAMA_MODEL=%r is not pulled on that Ollama (it has: %s); the assistant will answer 503. "
            "Run `ollama pull %s`, or set OLLAMA_MODEL to one of those.",
            config.OLLAMA_MODEL, ", ".join(pulled) or "nothing", config.OLLAMA_MODEL,
        )


@asynccontextmanager
async def _conversation_lifespan(_: FastAPI):
    """Hold the assistant's Redis connection (conversation memory, D63) open for the process."""
    assistant_deps.conversations.connect(f" (ttl {config.CONVERSATION_TTL_SECONDS}s)")
    if config.LLM_PROVIDER == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        deps.logger.warning(
            "ANTHROPIC_API_KEY is not set: the assistant endpoints will answer 503 "
            "until it is (search and RAG context are unaffected)"
        )
    if config.LLM_PROVIDER == "ollama":
        _warn_if_ollama_model_missing()
    try:
        yield
    finally:
        assistant_deps.conversations.close()


app = FastAPI(
    title="SmartFoodOps AI Service",
    lifespan=compose_lifespan(deps.db.lifespan, _vector_lifespan, _conversation_lifespan),
)
install_error_handlers(app)
instrument_app(app, deps.SERVICE_NAME)

app.include_router(health.router)
app.include_router(search.router)
app.include_router(rag.router)
app.include_router(assistant.router)
app.include_router(explain.router)
app.include_router(partner.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8009)
