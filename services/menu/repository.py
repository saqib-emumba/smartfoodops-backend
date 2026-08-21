"""PostgreSQL access for the `menus` table.

One row per restaurant, with the whole category tree in a JSONB column — the shape the
MongoDB document had, kept deliberately. Publishing is a single `ON CONFLICT` upsert
rather than a read-then-write, so two owners publishing at once cannot interleave into a
half-updated menu, and the UNIQUE constraint rather than the application is what enforces
"one live menu per restaurant".

`restaurant_id` is a plain UUID into the Restaurant Service's database; main.py verifies
it over HTTP before calling in here (see D02).
"""

from uuid import UUID

from psycopg2.extras import Json

from common.postgres import PostgresPool

_COLUMNS = "restaurant_id, categories"

_SELECT_BY_RESTAURANT = f"SELECT {_COLUMNS} FROM menus WHERE restaurant_id = %s"

_UPSERT_MENU = f"""
    INSERT INTO menus (restaurant_id, categories)
    VALUES (%s, %s)
    ON CONFLICT (restaurant_id) DO UPDATE
       SET categories = EXCLUDED.categories,
           updated_at = CURRENT_TIMESTAMP
    RETURNING {_COLUMNS}
"""


class MenuRepository:
    def __init__(self, db: PostgresPool):
        self._db = db

    def find(self, restaurant_id: UUID) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_BY_RESTAURANT, (str(restaurant_id),))
            return cur.fetchone()

    def upsert(self, restaurant_id: UUID, categories: list[dict]) -> dict:
        """Replace the category tree for one restaurant, inserting it if it is new."""
        with self._db.cursor(commit=True) as cur:
            cur.execute(_UPSERT_MENU, (str(restaurant_id), Json(categories)))
            return cur.fetchone()
