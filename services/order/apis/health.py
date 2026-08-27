"""Liveness probe for the Order Service.

Previously declared directly on `app` in main.py — the one health route in the platform not
on a router — because it and checkout.router's GET /{order_id} are both single-segment GET
paths under this prefix, and Starlette matches by registration order: a parameterised route
registered first would swallow /health and answer 422 trying to parse "health" as a UUID.
Same fix as everywhere else — register this router first — with no special case left in
main.py.

No `temporal_reachable` any more (D36): this service holds no Temporal client, so what it
can report is whether the Orchestrator Service answers over HTTP, the same shape every other
sibling dependency is reported in.
"""

from fastapi import APIRouter

from common.health import HealthResponse, health_payload
from common.responses import Envelope, ok
from order import deps

router = APIRouter(prefix="/api/v1/orders")


@router.get("/health", response_model=Envelope[HealthResponse])
async def health():
    payload = health_payload(
        deps.SERVICE_NAME,
        deps.db,
        user_service_url=deps.user_service.base_url,
        restaurant_service_url=deps.restaurant_service.base_url,
        menu_service_url=deps.menu_service.base_url,
        orchestrator_service_url=deps.orchestrator_service.base_url,
    )
    return ok(payload, message="Orders Service operational")
