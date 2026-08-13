"""PostgreSQL access for the `restaurants` table.

`owner_id` is a plain UUID column pointing into the User Service's database, so no foreign
key can validate it here. main.py verifies the owner over HTTP before calling in.
"""

from uuid import UUID

from common.postgres import PostgresPool
from schemas import RestaurantOnboardRequest

_COLUMNS = "id, owner_id, name, address, latitude, longitude, is_active, capacity"

_INSERT_RESTAURANT = f"""
    INSERT INTO restaurants (owner_id, name, address, latitude, longitude, capacity)
    VALUES (%s, %s, %s, %s, %s, %s)
    RETURNING {_COLUMNS}
"""

_SELECT_RESTAURANT = f"SELECT {_COLUMNS} FROM restaurants WHERE id = %s"


class RestaurantRepository:
    def __init__(self, db: PostgresPool):
        self._db = db

    def onboard(self, payload: RestaurantOnboardRequest) -> dict:
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                _INSERT_RESTAURANT,
                (
                    str(payload.owner_id),
                    payload.name,
                    payload.address,
                    payload.latitude,
                    payload.longitude,
                    payload.capacity,
                ),
            )
            return cur.fetchone()

    def find(self, restaurant_id: UUID) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_RESTAURANT, (str(restaurant_id),))
            return cur.fetchone()
