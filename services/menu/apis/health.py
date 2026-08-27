"""Liveness probe for the Menu Service.

Split out of menus.py so every service's health route lives in the same file name.
"""

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from menu import deps

router = APIRouter(prefix="/api/v1/menus")


@router.get("/health", response_model=Envelope[HealthResponse])
def health():
    payload = health_payload(
        deps.SERVICE_NAME,
        deps.db,
        cache_reachable=deps.cache.is_reachable(),
        restaurant_service_url=deps.restaurant_service.base_url,
    )
    return ok(payload, message="Menu Service running with PostgreSQL + Redis cache")
