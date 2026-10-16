"""Liveness probe for the AI Service.

Reports the embedding model alongside the database, since a service that is up but cannot
embed cannot answer any request that matters. The chat provider is named too (never its key), so
a test or an operator can tell whether answers come from a real model or the deterministic double.
"""

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from ai import assistant_deps, config, deps
from ai.llm import model_name

router = APIRouter(prefix="/api/v1/ai")


@router.get("/health", response_model=Envelope[HealthResponse])
def health():
    payload = health_payload(
        deps.SERVICE_NAME,
        deps.db,
        embedding_model=deps.embedder.name,
        embedding_dimensions=config.EMBEDDING_DIMENSIONS,
        llm_provider=config.LLM_PROVIDER,
        llm_model=model_name(),
        conversation_store_reachable=assistant_deps.conversations.is_reachable(),
    )
    return ok(payload, message="AI Service is operational")
