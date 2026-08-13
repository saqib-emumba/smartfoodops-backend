"""PostgreSQL access for the `payments` table.

A payment is written in two steps on purpose. The row is inserted as `pending` *before* the
gateway is called, so the unique index on `idempotency_key` — not application-level locking
— is what decides which of two concurrent retries gets to charge the card. Only after the
gateway answers is the row moved to `authorized` with its transaction reference.

`order_id` is a plain UUID column: it points into the Order Service's database, where no
foreign key can follow it, so main.py verifies the order over HTTP before calling in here.
"""

from decimal import Decimal
from logging import Logger
from uuid import UUID

import psycopg2
from fastapi import HTTPException

from common.errors import conflict
from common.postgres import PostgresPool
from schemas import PaymentCreateRequest

_COLUMNS = "id, order_id, amount, status, transaction_reference, idempotency_key"

_SELECT_BY_ID = f"SELECT {_COLUMNS} FROM payments WHERE id = %s"

_SELECT_BY_KEY = f"SELECT {_COLUMNS} FROM payments WHERE idempotency_key = %s"

_INSERT_PENDING = f"""
    INSERT INTO payments (order_id, amount, status, idempotency_key)
    VALUES (%s, %s, 'pending', %s)
    RETURNING {_COLUMNS}
"""

_MARK_AUTHORIZED = f"""
    UPDATE payments
       SET status = 'authorized',
           transaction_reference = %s,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = %s
    RETURNING {_COLUMNS}
"""


class PaymentRepository:
    def __init__(self, db: PostgresPool, *, logger: Logger):
        self._db = db
        self._logger = logger

    def find(self, payment_id: UUID) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_BY_ID, (str(payment_id),))
            return cur.fetchone()

    def find_by_idempotency_key(self, key: str) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_BY_KEY, (key,))
            return cur.fetchone()

    def create_pending(self, payload: PaymentCreateRequest, amount: Decimal) -> dict:
        """Claim the idempotency key with a `pending` row, before the card is charged."""
        with self._db.cursor(commit=True) as cur:
            try:
                cur.execute(
                    _INSERT_PENDING,
                    (str(payload.order_id), amount, payload.idempotency_key),
                )
            except psycopg2.errors.UniqueViolation as exc:
                raise self._duplicate(exc, payload) from exc
            return cur.fetchone()

    def mark_authorized(self, payment_id: UUID, reference: str) -> dict:
        """Record the gateway's verdict against the row that claimed the key."""
        with self._db.cursor(commit=True) as cur:
            cur.execute(_MARK_AUTHORIZED, (reference, str(payment_id)))
            return cur.fetchone()

    def _duplicate(
        self, exc: psycopg2.errors.UniqueViolation, payload: PaymentCreateRequest
    ) -> HTTPException:
        """Turn a unique-index violation into the 409 that describes what actually clashed.

        Two different collisions reach this point and the caller needs to tell them apart:
        a concurrent replay of the same key (retry later, nothing was charged twice) versus
        a second, differently-keyed attempt to pay one order (already paid for).
        """
        constraint = exc.diag.constraint_name or ""
        if "order_id" in constraint:
            self._logger.info("Order %s already has a payment", payload.order_id)
            return conflict(f"Order {payload.order_id} has already been paid for")
        self._logger.info("Concurrent replay for key %s", payload.idempotency_key)
        return conflict("A payment with this idempotency key is already being processed")
