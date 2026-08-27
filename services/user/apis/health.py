"""Liveness probe for the User Service.

Split out of users.py so every service's health route lives in the same file name.
"""

import os

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from user import deps

router = APIRouter(prefix="/api/v1/users")


@router.get("/health", response_model=Envelope[HealthResponse])
def health():
    """Liveness probe that also proves the database round-trips."""
    payload = health_payload(
        deps.SERVICE_NAME,
        deps.db,
        database_configured=bool(os.getenv("DATABASE_URL")),
        refresh_store_reachable=deps.refresh_tokens.is_reachable(),
    )
    return ok(payload, message="User Service is up and connected")
