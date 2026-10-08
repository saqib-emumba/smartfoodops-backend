"""HTTP routes for the `restaurants` resource: onboarding and lookup.

The health probe lives in health.py instead — see that module's docstring.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from common.auth import CurrentUser, get_current_user, require_internal, require_permission
from common.errors import not_found
from common.responses import Envelope, ok
from restaurant import deps
from restaurant.schemas.restaurants import RestaurantOnboardRequest, RestaurantResponse

router = APIRouter(prefix="/api/v1/restaurants")


@router.post(
    "/onboard",
    response_model=Envelope[RestaurantResponse],
    status_code=status.HTTP_201_CREATED,
)
def onboard_restaurant(
    payload: RestaurantOnboardRequest,
    current_user: CurrentUser = Depends(require_permission("restaurant:onboard")),
) -> Envelope[RestaurantResponse]:
    """Onboard a restaurant once its owner is verified through the User Service.

    The owner is the token's subject, so a restaurant can only ever be onboarded under the
    account making the request.
    """
    deps.user_service.verify_owner(current_user.user_id, current_user)
    row = deps.restaurants.onboard(payload, current_user.user_id)
    return ok(RestaurantResponse(**row), message="Restaurant onboarded", status=201)


@router.get("/{restaurant_id}", response_model=Envelope[RestaurantResponse])
def get_restaurant(
    restaurant_id: UUID,
    _: CurrentUser = Depends(get_current_user),
) -> Envelope[RestaurantResponse]:
    """Expose restaurant state (including is_active) for other services to verify.

    Any authenticated caller: customers browsing and the Order and Menu Services checking
    a restaurant all read the same non-sensitive record.
    """
    row = deps.restaurants.find(restaurant_id)
    if row is None:
        raise not_found(f"Restaurant {restaurant_id} not found")
    return ok(RestaurantResponse(**row), message="Restaurant found")


@router.get(
    "/{restaurant_id}/internal",
    response_model=Envelope[RestaurantResponse],
    dependencies=[Depends(require_internal)],
)
def get_restaurant_internally(restaurant_id: UUID) -> Envelope[RestaurantResponse]:
    """The same record, for callers with no user behind them — the AI Service's ingestion
    worker, which holds only the internal key (D15). Internal-key only, and with no
    `route_permissions` row, so unreachable through the gateway."""
    row = deps.restaurants.find(restaurant_id)
    if row is None:
        raise not_found(f"Restaurant {restaurant_id} not found")
    return ok(RestaurantResponse(**row), message="Restaurant found")
