"""PostgreSQL access for the `restaurants` table."""

from uuid import UUID

import psycopg2

from common.errors import not_found
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
            try:
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
            except psycopg2.errors.ForeignKeyViolation as exc:
                # Owner disappeared between verification and insert.
                raise not_found(f"Owner {payload.owner_id} does not exist") from exc
            return cur.fetchone()

    def find(self, restaurant_id: UUID) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_RESTAURANT, (str(restaurant_id),))
            return cur.fetchone()
