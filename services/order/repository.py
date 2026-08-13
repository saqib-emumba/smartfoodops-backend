"""PostgreSQL access for the `orders` table.

Idempotency is enforced by a unique index on `idempotency_key`, so a replay that loses the
race to a concurrent submission is resolved here rather than by application-level locking.

`customer_id` and `restaurant_id` are plain UUID columns: they point into other services'
databases, where no foreign key can follow them, so main.py verifies both over HTTP before
calling in here.
"""

from decimal import Decimal
from logging import Logger

import psycopg2
from psycopg2.extras import Json

from common.errors import conflict
from common.postgres import PostgresPool
from schemas import OrderCreateRequest

_COLUMNS = "id, customer_id, restaurant_id, items, total_amount, status, idempotency_key"

_SELECT_BY_KEY = f"SELECT {_COLUMNS} FROM orders WHERE idempotency_key = %s"

_INSERT_ORDER = f"""
    INSERT INTO orders (customer_id, restaurant_id, items, total_amount, status, idempotency_key)
    VALUES (%s, %s, %s, %s, 'created', %s)
    RETURNING {_COLUMNS}
"""


class OrderRepository:
    def __init__(self, db: PostgresPool, *, logger: Logger):
        self._db = db
        self._logger = logger

    def find_by_idempotency_key(self, key: str) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_BY_KEY, (key,))
            return cur.fetchone()

    def create(
        self,
        payload: OrderCreateRequest,
        items_snapshot: list[dict],
        total: Decimal,
        idempotency_key: str,
    ) -> dict:
        with self._db.cursor(commit=True) as cur:
            try:
                cur.execute(
                    _INSERT_ORDER,
                    (
                        str(payload.customer_id),
                        str(payload.restaurant_id),
                        Json(items_snapshot),
                        total,
                        idempotency_key,
                    ),
                )
            except psycopg2.errors.UniqueViolation as exc:
                # Concurrent submission won the race for this key.
                self._logger.info("Concurrent replay for key %s", idempotency_key)
                raise conflict(
                    "An order with this idempotency key is already being processed"
                ) from exc
            return cur.fetchone()
