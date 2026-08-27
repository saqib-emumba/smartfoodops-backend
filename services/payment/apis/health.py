"""Liveness probe for the Payment Service.

Split out of payments.py so every service's health route lives in the same file name.
"""

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from payment import deps

router = APIRouter(prefix="/api/v1/payments")


@router.get("/health", response_model=Envelope[HealthResponse])
def health():
    payload = health_payload(
        deps.SERVICE_NAME,
        deps.db,
        order_service_url=deps.order_service.base_url,
        gateway=deps.gateway.name,
    )
    return ok(payload, message="Payment Service is operational")
