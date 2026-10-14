"""Liveness probe for the AI Service.

Reports the embedding model alongside the database, since a service that is up but cannot
embed cannot answer any request that matters.
"""

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from ai import config, deps

router = APIRouter(prefix="/api/v1/ai")


@router.get("/health", response_model=Envelope[HealthResponse])
def health():
    payload = health_payload(
        deps.SERVICE_NAME,
        deps.db,
        embedding_model=deps.embedder.name,
        embedding_dimensions=config.EMBEDDING_DIMENSIONS,
    )
    return ok(payload, message="AI Service is operational")
