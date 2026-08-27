"""HTTP routes for the `restaurants` resource.

Two routes and a health probe, so one module. Both handler bodies are a verification call
and a repository call — there is nothing here that two routes share, and nothing that would
be clearer behind another layer.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from common.auth import CurrentUser, get_current_user, require_role
from common.errors import not_found
from common.health import health_payload
from restaurant import deps
from restaurant.schemas.restaurants import RestaurantOnboardRequest, RestaurantResponse

router = APIRouter(prefix="/api/v1/restaurants")


@router.get("/health")
def health():
    return health_payload(
        deps.SERVICE_NAME,
        deps.db,
        status="Restaurant Service is operational",
        user_service_url=deps.user_service.base_url,
    )


@router.post(
    "/onboard",
    response_model=RestaurantResponse,
    status_code=status.HTTP_201_CREATED,
)
def onboard_restaurant(
    payload: RestaurantOnboardRequest,
    current_user: CurrentUser = Depends(require_role("restaurant_admin")),
) -> RestaurantResponse:
    """Onboard a restaurant once its owner is verified through the User Service.

    The owner is the token's subject, so a restaurant can only ever be onboarded under the
    account making the request.
    """
    deps.user_service.verify_owner(current_user.user_id, current_user.token)
    return RestaurantResponse(
        **deps.restaurants.onboard(payload, current_user.user_id)
    )


@router.get("/{restaurant_id}", response_model=RestaurantResponse)
def get_restaurant(
    restaurant_id: UUID,
    _: CurrentUser = Depends(get_current_user),
) -> RestaurantResponse:
    """Expose restaurant state (including is_active) for other services to verify.

    Any authenticated caller: customers browsing and the Order and Menu Services checking
    a restaurant all read the same non-sensitive record.
    """
    row = deps.restaurants.find(restaurant_id)
    if row is None:
        raise not_found(f"Restaurant {restaurant_id} not found")
    return RestaurantResponse(**row)
