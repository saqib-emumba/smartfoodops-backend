"""Liveness probe for the Rider Service.

Split out of profile.py so every service's health route lives in the same file name.

`async def` as of D47, for the same reason the Order Service's is: this service now holds a
Temporal client, and asking whether it answers is awaited.
"""

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from rider import deps

router = APIRouter(prefix="/api/v1/riders")


@router.get("/health", response_model=Envelope[HealthResponse])
async def health():
    payload = health_payload(
        deps.SERVICE_NAME,
        deps.db,
        user_service_url=deps.user_service.base_url,
        order_service_url=deps.order_service.base_url,
        temporal_reachable=await deps.temporal.is_reachable(),
        temporal_address=deps.temporal.address,
    )
    return ok(payload, message="Rider Service is operational")
