"""PostgreSQL access for the `restaurants` table.

`owner_id` is a plain UUID column pointing into the User Service's database, so no foreign
key can validate it here. main.py verifies the owner over HTTP before calling in.

An `order_tickets` queue lived here through the first cut of Week 2. D32 moved the kitchen's
decision onto `orders.kitchen_decision` in the Order Service's database, where the rest of
an order's lifecycle already lived — so this service is back to owning exactly one table.
"""

from uuid import UUID

from common.repository import Repository
from restaurant.schemas.restaurants import RestaurantOnboardRequest

_COLUMNS = "id, owner_id, name, address, latitude, longitude, is_active, capacity, prep_time_minutes"

_INSERT_RESTAURANT = f"""
    INSERT INTO restaurants (owner_id, name, address, latitude, longitude, capacity, prep_time_minutes)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    RETURNING {_COLUMNS}
"""

_SELECT_RESTAURANT = f"SELECT {_COLUMNS} FROM restaurants WHERE id = %s"

_UPDATE_PREP_TIME = f"""
    UPDATE restaurants SET prep_time_minutes = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s
    RETURNING {_COLUMNS}
"""


class RestaurantRepository(Repository):
    def onboard(self, payload: RestaurantOnboardRequest, owner_id: UUID) -> dict:
        """Insert a restaurant. `owner_id` is passed separately because it comes from the
        access token rather than the request body — see main.onboard_restaurant."""
        return self.write_one(
            _INSERT_RESTAURANT,
            (
                str(owner_id),
                payload.name,
                payload.address,
                payload.latitude,
                payload.longitude,
                payload.capacity,
                payload.prep_time_minutes,
            ),
        )

    def find(self, restaurant_id: UUID) -> dict | None:
        return self.one(_SELECT_RESTAURANT, (str(restaurant_id),))

    def set_prep_time(self, restaurant_id: UUID, minutes: int) -> dict | None:
        return self.write_one(_UPDATE_PREP_TIME, (minutes, str(restaurant_id)))
