"""HTTP routes for the `menus` resource: publish and read.

The health probe lives in health.py instead — see that module's docstring. The publish path
and the read path share `_as_response` and nothing else, so it stays here beside both of them
rather than moving to a file of its own.
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from common.auth import CurrentUser, get_current_user, require_internal, require_permission
from common.errors import forbidden, not_found
from common.events.topics import MENU_EVENTS_TOPIC
from common.responses import Envelope, ok
from menu import deps
from menu.schemas.menus import (
    InternalMenuResponse,
    MenuResponse,
    MenuUpsertRequest,
    PublishedMenuRef,
)

router = APIRouter(prefix="/api/v1/menus")


def _as_response(row: dict) -> MenuResponse:
    return MenuResponse(
        restaurant_id=row["restaurant_id"], categories=row["categories"]
    )


@router.post("", response_model=Envelope[MenuResponse])
async def upsert_menu(
    payload: MenuUpsertRequest,
    current_user: CurrentUser = Depends(require_permission("menu:write")),
) -> Envelope[MenuResponse]:
    """Upsert the full category/item/customization tree for one restaurant.

    Holding the `restaurant_admin` role is not enough — the caller must own *this*
    restaurant, or any restaurant admin could rewrite a competitor's prices.

    Async only because the ownership check is an outbound HTTP call; the database work
    below is blocking and brief, which is the same trade the write path always made.
    """
    restaurant = await deps.restaurant_service.verify_active(
        payload.restaurant_id, current_user
    )
    if str(restaurant.get("owner_id")) != str(current_user.user_id) and not current_user.is_admin:
        raise forbidden(f"You do not own restaurant {payload.restaurant_id}")

    categories = [category.model_dump() for category in payload.categories]
    row = deps.menus.upsert(payload.restaurant_id, categories)

    # Invalidate only after the row is committed. Dropping the key first would let a
    # concurrent reader repopulate the cache from the old row and leave the stale copy
    # behind the write that was supposed to replace it.
    deps.cache.invalidate(payload.restaurant_id)
    await _announce_published(payload.restaurant_id, row)
    return ok(_as_response(row), message="Menu published")


async def _announce_published(restaurant_id: UUID, row: dict) -> None:
    """Tell the AI Service's ingestion worker this restaurant's menu changed (D61).

    After the commit and the cache invalidation, and swallowing every failure: the owner's
    publish has already succeeded and must not turn into a 5xx because a broker was down.
    The event is a hint to re-read, not the data, so a lost one costs freshness only — the
    worker's startup backfill closes the gap.
    """
    try:
        items_count = sum(len(category["items"]) for category in row["categories"])
        await deps.kafka.publish(
            MENU_EVENTS_TOPIC,
            aggregate_type="menu",
            aggregate_id=str(restaurant_id),
            event_type="menu.published",
            payload={
                "restaurant_id": str(restaurant_id),
                "published_at": row["updated_at"].isoformat(),
                "items_count": items_count,
            },
            event_key=row["updated_at"].isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - see docstring: a publish must not fail on Kafka
        deps.logger.warning("menu.published for %s was not announced: %s", restaurant_id, exc)


@router.get(
    "/internal/restaurant-ids",
    response_model=Envelope[list[PublishedMenuRef]],
    dependencies=[Depends(require_internal)],
)
def list_published_menus() -> Envelope[list[PublishedMenuRef]]:
    """Every restaurant with a published menu — what the AI Service's backfill walks.

    Internal-key only, and unreachable through the gateway: it has no `route_permissions` row,
    so the gateway refuses it (D57) however privileged the bearer token.
    """
    rows = deps.menus.list_published()
    return ok([PublishedMenuRef(**row) for row in rows], message="Published menus")


@router.get(
    "/{restaurant_id}/internal",
    response_model=Envelope[InternalMenuResponse],
    dependencies=[Depends(require_internal)],
)
def get_menu_internally(restaurant_id: UUID) -> Envelope[InternalMenuResponse]:
    """The menu with its version stamp, for the AI Service's ingestion worker.

    Reads Postgres directly and never the cache: the cache holds the public `MenuResponse`
    shape without `updated_at`, and an ingestion triggered by a publish must see that publish,
    not a copy that may predate it.
    """
    row = deps.menus.find(restaurant_id)
    if row is None:
        raise not_found(f"No menu published for restaurant {restaurant_id}")
    return ok(
        InternalMenuResponse(
            restaurant_id=row["restaurant_id"],
            categories=row["categories"],
            updated_at=row["updated_at"],
        ),
        message="Menu found",
    )


@router.get("/{restaurant_id}", response_model=Envelope[MenuResponse])
def get_menu(
    restaurant_id: UUID,
    _: CurrentUser = Depends(get_current_user),
) -> Envelope[MenuResponse]:
    """Serve the active menu tree — used by the Order Service to price a checkout.

    Cache-aside: Redis, then Postgres, then populate. Every checkout reads this endpoint,
    so the hot path is a single key lookup; a cache miss or a Redis outage costs one query
    rather than an error, because the cache is a copy and never the source of truth.

    Any authenticated caller: customers browse it, and the Order Service reads it while
    forwarding the customer's own token.

    The cache still stores the bare `MenuResponse` JSON, not the envelope — envelope
    wrapping happens once, here, at the HTTP boundary; the cached shape is this service's
    own business and unrelated to what a caller sees on the wire.
    """
    cached = deps.cache.get(restaurant_id)
    if cached is not None:
        try:
            return ok(MenuResponse.model_validate_json(cached), message="Menu found")
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
    return ok(menu, message="Menu found")
