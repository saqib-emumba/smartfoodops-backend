"""PostgreSQL access for the `payments` table.

A payment is written in two steps on purpose. The row is inserted as `pending` *before* the
gateway is called, so the unique index on `idempotency_key` — not application-level locking
— is what decides which of two concurrent retries gets to charge the card. Only after the
gateway answers is the row moved to `authorized` with its transaction reference.

`order_id` is a plain UUID column: it points into the Order Service's database, where no
foreign key can follow it, so main.py verifies the order over HTTP before calling in here.
"""

from decimal import Decimal
from uuid import UUID

import psycopg2
from fastapi import HTTPException

from common.errors import conflict
from common.outbox import append_outbox
from common.postgres import constraint_of
from common.repository import Repository
from payment.schemas.payments import PaymentAuthorizeRequest, PaymentCreateRequest

_COLUMNS = "id, order_id, amount, status, transaction_reference, idempotency_key"

_SELECT_BY_ID = f"SELECT {_COLUMNS} FROM payments WHERE id = %s"

_SELECT_BY_KEY = f"SELECT {_COLUMNS} FROM payments WHERE idempotency_key = %s"

_SELECT_BY_ORDER = f"SELECT {_COLUMNS} FROM payments WHERE order_id = %s"

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

# Guarded on status rather than blind, because Temporal retries the compensating activity
# and a second refund is real money. A row already `refunded` matches nothing here, so the
# caller reads the existing row instead of issuing another gateway call.
#
# `pending` is included alongside `authorized` on purpose: a payment whose gateway call
# failed is exactly the stranded row D10 accepted and nothing has ever swept. The saga's
# refund is what finally resolves them.
_MARK_REFUNDED = f"""
    UPDATE payments
       SET status = 'refunded',
           transaction_reference = %s,
           updated_at = CURRENT_TIMESTAMP
     WHERE order_id = %s
       AND status IN ('pending', 'authorized', 'captured')
    RETURNING {_COLUMNS}
"""


class PaymentRepository(Repository):
    def find(self, payment_id: UUID) -> dict | None:
        return self.one(_SELECT_BY_ID, (str(payment_id),))

    def find_by_idempotency_key(self, key: str) -> dict | None:
        return self.one(_SELECT_BY_KEY, (key,))

    def find_by_order(self, order_id: UUID) -> dict | None:
        """The one payment for an order, if any. `order_id` is UNIQUE, so at most one."""
        return self.one(_SELECT_BY_ORDER, (str(order_id),))

    def create_pending(
        self, payload: PaymentCreateRequest | PaymentAuthorizeRequest, amount: Decimal
    ) -> dict:
        """Claim the idempotency key with a `pending` row, before the card is charged.

        Takes either request shape: the customer-facing endpoint and the saga's authorise
        endpoint differ in how they are authenticated and in how `amount` arrives on the
        wire, but both carry an `order_id` and an `idempotency_key`, which is all the
        insert needs.
        """
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
        """Record the gateway's verdict against the row that claimed the key.

        Was a single-statement `write_one`; now an explicit transaction so the
        `payment.authorized` outbox row commits atomically with the status change (D39) —
        the same argument D24 made for order_tracking_logs, applied to this table's first
        outbox write. `authorise()` only reaches this method on the non-replay path (it
        returns early on a replay before ever calling here), so "emit once per real
        authorisation" falls out of the existing call structure rather than needing a
        second guard here.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(_MARK_AUTHORIZED, (reference, str(payment_id)))
            payment = self._row(cur.fetchone())
            append_outbox(
                cur,
                table="payment_outbox",
                aggregate_type="payment",
                aggregate_id=payment["order_id"],
                event_type="payment.authorized",
                payload={
                    "payment_id": str(payment["id"]),
                    "order_id": str(payment["order_id"]),
                    "amount": payment["amount"],
                    "transaction_reference": payment["transaction_reference"],
                },
            )
            return payment

    def mark_refunded(self, order_id: UUID, reference: str) -> dict | None:
        """Move an order's payment to `refunded`, if it is in a state that can be.

        Returns None when nothing was refundable — either there is no payment, or it is
        already `refunded`. The caller distinguishes those, because the second is success
        for a compensating action and the first is worth saying out loud. The outbox row
        is written only in the branch that actually changed something — a payment already
        `refunded` reaches this method's *caller*'s "no-op" path, never this one, so a
        second refund attempt (Temporal retries the compensating activity) cannot double-
        emit.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(_MARK_REFUNDED, (reference, str(order_id)))
            row = cur.fetchone()
            if row is None:
                return None
            payment = self._row(row)
            append_outbox(
                cur,
                table="payment_outbox",
                aggregate_type="payment",
                aggregate_id=order_id,
                event_type="payment.refunded",
                payload={
                    "payment_id": str(payment["id"]),
                    "order_id": str(payment["order_id"]),
                    "amount": payment["amount"],
                    "transaction_reference": payment["transaction_reference"],
                },
            )
            return payment

    def _duplicate(
        self,
        exc: psycopg2.errors.UniqueViolation,
        payload: PaymentCreateRequest | PaymentAuthorizeRequest,
    ) -> HTTPException:
        """Turn a unique-index violation into the 409 that describes what actually clashed.

        Two different collisions reach this point and the caller needs to tell them apart:
        a concurrent replay of the same key (retry later, nothing was charged twice) versus
        a second, differently-keyed attempt to pay one order (already paid for).
        """
        constraint = constraint_of(exc)
        if "order_id" in constraint:
            self._logger.info("Order %s already has a payment", payload.order_id)
            return conflict(f"Order {payload.order_id} has already been paid for")
        self._logger.info("Concurrent replay for key %s", payload.idempotency_key)
        return conflict("A payment with this idempotency key is already being processed")
