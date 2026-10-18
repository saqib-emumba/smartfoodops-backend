"""The Restaurant Service, as the ingestion worker reads it (internal route, D61)."""

from common.auth import CurrentUser, identity_headers, internal_headers, require_self_or_admin
from common.config import DEFAULT_RESTAURANT_SERVICE_URL
from common.service_client import ServiceFacade


class RestaurantServiceClient(ServiceFacade):
    display_name = "Restaurant Service"
    env_var = "RESTAURANT_SERVICE_URL"
    default_url = DEFAULT_RESTAURANT_SERVICE_URL

    def get_restaurant(self, restaurant_id: str) -> dict:
        return self._client.get(
            f"/api/v1/restaurants/{restaurant_id}/internal",
            missing=f"Restaurant {restaurant_id} not found",
            unreachable_hint="cannot read the restaurant",
            headers=internal_headers(),
        )

    def names(self, restaurant_ids: list[str]) -> dict[str, dict]:
        """`{restaurant_id: {name, is_active}}` for whichever of these still exist, read live.

        Puts names on the analytics service's favourite-restaurant ids: its projection keeps only
        ids, and the Restaurant Service is where a name lives. One lookup per id, in parallel; a
        restaurant that is gone (a `404`) is simply absent from the result.
        """
        from concurrent.futures import ThreadPoolExecutor

        from fastapi import HTTPException

        def read(rid: str):
            try:
                row = self.get_restaurant(rid)
            except HTTPException as exc:
                if exc.status_code == 404:
                    return rid, None
                raise
            return rid, {"name": row["name"], "is_active": row["is_active"]}

        if not restaurant_ids:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(restaurant_ids))) as pool:
            return {rid: row for rid, row in pool.map(read, restaurant_ids) if row is not None}

    def verify_owner(self, restaurant_id: str, current_user: CurrentUser) -> dict:
        """Confirm the caller owns this restaurant (or is an admin), asserting their own identity.

        The partner tools write copy on a restaurant's behalf, so they are gated on ownership,
        which is the Restaurant Service's fact (D16). Same shape as the Order Service's
        `RestaurantServiceClient.verify_owner`; routed through `require_self_or_admin` so the
        `system_admin` bypass is the platform's usual one (D33).
        """
        restaurant = self._client.get(
            f"/api/v1/restaurants/{restaurant_id}",
            missing=f"Restaurant {restaurant_id} not found",
            unreachable_hint="cannot verify who owns this restaurant",
            headers=identity_headers(current_user),
        )
        require_self_or_admin(
            current_user,
            restaurant.get("owner_id"),
            detail=f"You do not own restaurant {restaurant_id}",
        )
        return restaurant
