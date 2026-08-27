"""Liveness probe for the Restaurant Service.

Split out of restaurants.py so every service's health route lives in the same
file name — see order/main.py's docstring for why order was the odd one out before this.
"""

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from restaurant import deps

router = APIRouter(prefix="/api/v1/restaurants")


@router.get("/health", response_model=Envelope[HealthResponse])
def health():
    payload = health_payload(
        deps.SERVICE_NAME,
        deps.db,
        user_service_url=deps.user_service.base_url,
    )
    return ok(payload, message="Restaurant Service is operational")
