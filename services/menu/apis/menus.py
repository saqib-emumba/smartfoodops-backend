"""HTTP routes for the `menus` resource.

Two routes and a health probe, so one module. The publish path and the read path share
`_as_response` and nothing else, so it stays here beside both of them rather than moving to
a file of its own.
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from common.auth import CurrentUser, get_current_user, require_role
from common.errors import forbidden, not_found
from common.health import health_payload
from menu import deps
from menu.schemas.menus import MenuResponse, MenuUpsertRequest

router = APIRouter(prefix="/api/v1/menus")


def _as_response(row: dict) -> MenuResponse:
    return MenuResponse(
        restaurant_id=row["restaurant_id"], categories=row["categories"]
    )


@router.get("/health")
def health():
    return health_payload(
        deps.SERVICE_NAME,
        deps.db,
        status="Menu Service running with PostgreSQL + Redis cache",
        cache_reachable=deps.cache.is_reachable(),
        restaurant_service_url=deps.restaurant_service.base_url,
    )


@router.post("", response_model=MenuResponse)
async def upsert_menu(
    payload: MenuUpsertRequest,
    current_user: CurrentUser = Depends(require_role("restaurant_admin")),
) -> MenuResponse:
    """Upsert the full category/item/customization tree for one restaurant.

    Holding the `restaurant_admin` role is not enough — the caller must own *this*
    restaurant, or any restaurant admin could rewrite a competitor's prices.

    Async only because the ownership check is an outbound HTTP call; the database work
    below is blocking and brief, which is the same trade the write path always made.
    """
    restaurant = await deps.restaurant_service.verify_active(
        payload.restaurant_id, current_user.token
    )
    if str(restaurant.get("owner_id")) != str(current_user.user_id) and not current_user.is_admin:
        raise forbidden(f"You do not own restaurant {payload.restaurant_id}")

    categories = [category.model_dump() for category in payload.categories]
    row = deps.menus.upsert(payload.restaurant_id, categories)

    # Invalidate only after the row is committed. Dropping the key first would let a
    # concurrent reader repopulate the cache from the old row and leave the stale copy
    # behind the write that was supposed to replace it.
    deps.cache.invalidate(payload.restaurant_id)
    return _as_response(row)


@router.get("/{restaurant_id}", response_model=MenuResponse)
def get_menu(
    restaurant_id: UUID,
    _: CurrentUser = Depends(get_current_user),
) -> MenuResponse:
    """Serve the active menu tree — used by the Order Service to price a checkout.

    Cache-aside: Redis, then Postgres, then populate. Every checkout reads this endpoint,
    so the hot path is a single key lookup; a cache miss or a Redis outage costs one query
    rather than an error, because the cache is a copy and never the source of truth.

    Any authenticated caller: customers browse it, and the Order Service reads it while
    forwarding the customer's own token.
    """
    cached = deps.cache.get(restaurant_id)
    if cached is not None:
        try:
            return MenuResponse.model_validate_json(cached)
        except ValidationError as exc:
            # A payload written by an older build of this service. Treat it as a miss and
            # let the read below overwrite it rather than failing a request over it.
            deps.logger.warning(
                "Discarding unreadable cached menu %s: %s", restaurant_id, exc
            )

    row = deps.menus.find(restaurant_id)
    if row is None:
        raise not_found(f"No menu published for restaurant {restaurant_id}")

    menu = _as_response(row)
    deps.cache.store(restaurant_id, menu.model_dump_json())
    return menu
