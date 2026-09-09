"""Liveness probe for the Analytics Service.

Same shape as every other service's health route — `health_payload`'s `**extra` accepts
new fields with no schema change (`common/health.py`'s `HealthResponse` is
`ConfigDict(extra="allow")` for exactly this) — but the extra here is the consumer's own
liveness, not a sibling URL: a service with no siblings to call has nothing else to report.
"""

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from analytics import deps

router = APIRouter(prefix="/api/v1/analytics")


@router.get("/health", response_model=Envelope[HealthResponse])
def health():
    payload = health_payload(deps.SERVICE_NAME, deps.db)
    return ok(payload, message="Analytics Service is operational")
