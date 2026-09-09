"""Liveness probe for the Order Service.

Previously declared directly on `app` in main.py — the one health route in the platform not
on a router — because it and checkout.router's GET /{order_id} are both single-segment GET
paths under this prefix, and Starlette matches by registration order: a parameterised route
registered first would swallow /health and answer 422 trying to parse "health" as a UUID.
Same fix as everywhere else — register this router first — with no special case left in
main.py.

`temporal_reachable` again (D47), having been replaced by an `orchestrator_service_url` for
the span of D36: this service holds a Temporal client once more, so what it can report is
whether Temporal itself answers rather than whether a facade in front of it does.
`common/health.py`'s own docstring — "The Order Service's route stays `async` because it
awaits Temporal" — was written for the first arrangement and is accurate again.
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
        temporal_reachable=await deps.temporal.is_reachable(),
        temporal_address=deps.temporal.address,
    )
    return ok(payload, message="Orders Service operational")
