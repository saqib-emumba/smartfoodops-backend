"""Liveness probe for the Orchestrator Service.

No `database_reachable`: this is the one service in the platform with no database of its
own (D36) — `common.health.health_payload` omits the key entirely when `db` is `None`
rather than reporting a `False` that would misread as a degraded service.
"""

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from orchestrator import deps

router = APIRouter(prefix="/api/v1/orchestrator")


@router.get("/health", response_model=Envelope[HealthResponse])
async def health():
    payload = health_payload(
        deps.SERVICE_NAME,
        None,
        temporal_reachable=await deps.temporal.is_reachable(),
        temporal_address=deps.temporal.address,
    )
    return ok(payload, message="Orchestrator Service operational")
