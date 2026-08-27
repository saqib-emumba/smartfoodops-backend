"""The restaurant an order names, and who owns it, verified against the Restaurant Service."""

from uuid import UUID

from common.auth import CurrentUser, bearer, require_self_or_admin
from common.config import DEFAULT_RESTAURANT_SERVICE_URL
from common.errors import unprocessable
from common.service_client import ServiceFacade


class RestaurantServiceClient(ServiceFacade):
    display_name = "Restaurant Service"
    env_var = "RESTAURANT_SERVICE_URL"
    default_url = DEFAULT_RESTAURANT_SERVICE_URL

    def verify_owner(self, restaurant_id: UUID, current_user: CurrentUser) -> dict:
        """Confirm this caller owns the restaurant an order was placed with, or is an admin.

        Who owns a restaurant is the Restaurant Service's fact, so it is resolved there
        rather than duplicated here (D16) — this reads the record as the caller and compares
        the owner, exactly as the Menu Service does before letting an admin publish a menu.

        Routed through `require_self_or_admin` rather than a hand-rolled comparison so the
        `system_admin` bypass is the one everywhere else in the platform honours (D33) —
        this used to compare `owner_id` directly and never admitted an admin, the one place
        the bypass did not hold.

        Forwards the caller's own bearer token rather than the internal key: the caller *is*
        a user, so the call should be able to do no more than they could (D15).
        """
        restaurant = self._client.get(
            f"/api/v1/restaurants/{restaurant_id}",
            missing=f"Restaurant {restaurant_id} no longer exists",
            unreachable_hint="cannot verify who owns this restaurant",
            bad_gateway_hint="verifying restaurant ownership",
            headers=bearer(current_user.token),
        )
        require_self_or_admin(
            current_user,
            restaurant.get("owner_id"),
            detail=f"You do not own restaurant {restaurant_id}, so you may not decide its orders",
        )
        return restaurant

    def verify_restaurant(self, restaurant_id: UUID, token: str) -> dict:
        """Confirm the restaurant exists — the check the `restaurant_id` foreign key made.

        Existence only: whether a restaurant may currently take orders is the Menu
        Service's call, and an unpublished or withdrawn menu already fails the re-pricing
        step that runs before this check.
        """
        return self._client.get(
            f"/api/v1/restaurants/{restaurant_id}",
            missing=f"Unknown restaurant {restaurant_id} referenced by this order",
            missing_error=unprocessable,
            unreachable_hint="cannot verify the restaurant",
            bad_gateway_hint="verifying the restaurant",
            headers=bearer(token),
        )
