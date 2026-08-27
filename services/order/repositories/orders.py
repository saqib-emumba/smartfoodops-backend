"""The `orders` table: checkout, transitions, and the kitchen's view of it.

`customer_id` and `restaurant_id` are plain UUID columns: they point into other services'
databases, where no foreign key can follow them, so api/checkout.py verifies both over HTTP
before calling in here. `customer_id` additionally never comes from the client — it is the
subject of the verified access token.
"""

import json
from decimal import Decimal
from logging import Logger
from uuid import UUID

import psycopg2
from psycopg2.extras import Json

from common.errors import conflict
from common.postgres import PostgresPool
from common.repository import Repository
from order.repositories.sql import (
    COUNT_ON_RAIL_FOR_ORDER,
    DECIDE_KITCHEN,
    INSERT_ORDER,
    SELECT_BY_ID,
    SELECT_BY_KEY,
    SELECT_KITCHEN_QUEUE,
    TRANSITION_ORDER,
    append_log,
)
from order.schemas.orders import OrderCreateRequest


class AtCapacity(Exception):
    """The kitchen already has as many undecided orders as its capacity allows.

    A business answer, not a failure — which is why it is a distinct exception rather than
    a generic error. The saga raises it non-retryably: waiting and asking again cannot
    change a refusal the restaurant has already given.
    """

    def __init__(self, order_id, on_rail: int, capacity: int):
        super().__init__(
            f"The kitchen for order {order_id} has {on_rail} orders awaiting a "
            f"decision and a capacity of {capacity}"
        )
        self.on_rail = on_rail
        self.capacity = capacity


class OrderRepository(Repository):
    def __init__(self, db: PostgresPool, *, logger: Logger, service_name: str):
        super().__init__(db, logger=logger)
        # Recorded on the entries this service writes, so a trail read back later says
        # which service observed each transition.
        self._service_name = service_name

    def find(self, order_id: UUID) -> dict | None:
        return self.one(SELECT_BY_ID, (str(order_id),))

    def find_by_idempotency_key(self, key: str) -> dict | None:
        return self.one(SELECT_BY_KEY, (key,))

    def create(
        self,
        payload: OrderCreateRequest,
        customer_id: UUID,
        items_snapshot: list[dict],
        total: Decimal,
        idempotency_key: str,
    ) -> dict:
        """Insert an order and open its audit trail in one transaction.

        `customer_id` is passed separately because it comes from the access token rather
        than the request body — see api/checkout.py.

        The `created` entry used to be an HTTP call to the Menu Service made after the
        commit, which could only ever be best-effort: the order already existed, so a
        failed log had to be swallowed. Now that both tables share a database the two
        writes commit together — an order without its first transition cannot exist, and a
        log that cannot be written takes the order down with it, which is safe because
        nothing has been committed for the client to have been told about.
        """
        with self._db.cursor(commit=True) as cur:
            try:
                cur.execute(
                    INSERT_ORDER,
                    (
                        str(customer_id),
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
            order = cur.fetchone()

            append_log(
                cur,
                {
                    "order_id": str(order["id"]),
                    "new_status": order["status"],
                    "service": self._service_name,
                    "updated_by": "customer_client",
                    "raw_log": json.dumps(
                        {
                            "event": "order_created",
                            "order_id": str(order["id"]),
                            "total_amount": float(order["total_amount"]),
                            "items_count": len(order["items"]),
                        }
                    ),
                    "metadata": Json({"idempotency_key": idempotency_key}),
                },
            )
            return order

    def transition(
        self,
        *,
        order_id: UUID | str,
        new_status: str,
        updated_by: str = "system",
        event: dict | None = None,
        metadata: dict | None = None,
        rider_id: UUID | str | None = None,
        capacity_limit: int | None = None,
    ) -> tuple[dict | None, bool]:
        """Advance an order and record the transition, in one transaction.

        Returns `(order, changed)`. `changed` is False when the compare-and-set matched
        nothing — the order is already in that state, or has reached a terminal one — and
        in that case **no trail entry is written**. That is the whole reason the two writes
        are guarded together rather than separately: an activity retried five times must
        leave one entry, not five.

        `old_status` is deliberately not a parameter. It is derived from the preceding entry
        by the insert itself, so the chain cannot disagree with itself (D24). The first
        revision of the Week 2 blueprint let the workflow assert a previous status, which a
        retry or a reordered activity could contradict.

        Returns `(None, False)` when the order does not exist at all, which the caller
        distinguishes from a no-op because they mean different things to a saga.

        `capacity_limit` gates entry into `confirmed`, and it is here rather than in a
        method of its own because it has to be *atomic with the transition*: entering
        `confirmed` is what puts an order on the kitchen's rail, so counting the rail and
        joining it must not be two statements two orders can interleave between. Raises
        `AtCapacity` when full, having written nothing.
        """
        with self._db.cursor(commit=True) as cur:
            if capacity_limit is not None:
                cur.execute(COUNT_ON_RAIL_FOR_ORDER, {"order_id": str(order_id)})
                on_rail = cur.fetchone()["on_rail"]
                if on_rail >= capacity_limit:
                    raise AtCapacity(order_id, on_rail, capacity_limit)

            cur.execute(
                TRANSITION_ORDER,
                {
                    "order_id": str(order_id),
                    "new_status": new_status,
                    "rider_id": str(rider_id) if rider_id else None,
                },
            )
            updated = cur.fetchone()

            if updated is None:
                cur.execute(SELECT_BY_ID, (str(order_id),))
                current = cur.fetchone()
                if current is not None:
                    self._logger.info(
                        "Order %s is %s; transition to %s is a no-op",
                        order_id,
                        current["status"],
                        new_status,
                    )
                return current, False

            append_log(
                cur,
                {
                    "order_id": str(order_id),
                    "new_status": new_status,
                    "service": self._service_name,
                    "updated_by": updated_by,
                    "raw_log": json.dumps(event or {"event": f"order_{new_status}"}),
                    "metadata": Json(metadata or {}),
                },
            )
            return updated, True

    def kitchen_queue(self, restaurant_id: UUID) -> list[dict]:
        """Orders awaiting this kitchen's decision, oldest first."""
        return self.all(SELECT_KITCHEN_QUEUE, (str(restaurant_id),))

    def decide_kitchen(self, order_id: UUID, decision: str) -> tuple[dict | None, bool]:
        """Record the kitchen's accept or reject.

        Returns `(order, changed)`. `changed` is False when the order was already decided,
        or is no longer `confirmed` — which lets the caller signal the saga exactly once. A
        second accept must not tell the workflow twice, and a click on an order the saga
        already timed out and cancelled must not un-cancel it.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                DECIDE_KITCHEN, {"order_id": str(order_id), "decision": decision}
            )
            decided = cur.fetchone()
            if decided is not None:
                return decided, True
            cur.execute(SELECT_BY_ID, (str(order_id),))
            return cur.fetchone(), False
