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
    INSERT_LINE_ITEM,
    INSERT_LINE_ITEM_OPTION,
    INSERT_ORDER,
    RECORD_RIDER_REPORT,
    SELECT_BY_ID,
    SELECT_BY_KEY,
    SELECT_KITCHEN_QUEUE,
    SELECT_LINE_ITEM_OPTIONS_FOR_LINE_ITEMS,
    SELECT_LINE_ITEMS_FOR_ORDERS,
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
        order = self.one(SELECT_BY_ID, (str(order_id),))
        if order is not None:
            self._attach_items([order])
        return order

    def find_by_idempotency_key(self, key: str) -> dict | None:
        order = self.one(SELECT_BY_KEY, (key,))
        if order is not None:
            self._attach_items([order])
        return order

    def _attach_items(self, orders: list[dict]) -> None:
        """Merge each order's line items (with their chosen options) onto it, in place.

        `items` stopped being a column in D50 — split into order_line_items and
        order_line_item_options so a price or a selection can be constrained and queried
        by the engine instead of trusted to Pydantic alone. Batched over every order in
        `orders` rather than called once per order, so kitchen_queue's N orders cost two
        queries total, not 2N.
        """
        if not orders:
            return
        order_ids = [str(order["id"]) for order in orders]
        line_item_rows = self.all(SELECT_LINE_ITEMS_FOR_ORDERS, {"order_ids": order_ids})
        line_item_ids = [str(row["id"]) for row in line_item_rows]
        option_rows = (
            self.all(SELECT_LINE_ITEM_OPTIONS_FOR_LINE_ITEMS, {"line_item_ids": line_item_ids})
            if line_item_ids
            else []
        )

        options_by_line_item: dict[str, list[dict]] = {}
        for option in option_rows:
            options_by_line_item.setdefault(str(option["line_item_id"]), []).append(
                {
                    "group_id": option["group_key"],
                    "name": option["name"],
                    "extra_price": float(option["extra_price"]),
                }
            )

        items_by_order: dict[str, list[dict]] = {}
        for row in line_item_rows:
            items_by_order.setdefault(str(row["order_id"]), []).append(
                {
                    "item_id": row["menu_item_id"],
                    "name": row["item_name"],
                    "quantity": row["quantity"],
                    "customizations": row["customizations"],
                    "unit_price": float(row["unit_price"]),
                    "line_total": float(row["line_total"]),
                    "selected_options": options_by_line_item.get(str(row["id"]), []),
                }
            )

        for order in orders:
            order["items"] = items_by_order.get(str(order["id"]), [])

    def create(
        self,
        order_id: UUID,
        payload: OrderCreateRequest,
        customer_id: UUID,
        items_snapshot: list[dict],
        total: Decimal,
        idempotency_key: str,
    ) -> tuple[dict, bool]:
        """Insert an order and open its audit trail in one transaction.

        `order_id` arrives from the caller rather than the column's own default
        (order-creation-temporal-update-design.md ss3): it is derived deterministically from
        `(customer_id, idempotency_key)` before this is ever called, so the Temporal Update
        that runs this can be addressed by an id nobody has generated yet. `customer_id` is
        passed separately because it comes from the access token rather than the request
        body — see apis/internal_orders.py.

        Returns `(order, created)`. `created` is False when two concurrent requests for the
        same `(customer_id, idempotency_key)` both reached this method before either
        committed (D53 ss4, possible now that creation runs inside a workflow Update instead
        of one synchronous call): both derive the same `order_id`, so the loser's insert
        collides on the primary key rather than `idempotency_key`, and re-selecting the row
        the winner just committed is the correct answer, not an error — the same
        `201`-first/`200`-after semantics ([D08](../../readme/key-decisions.md#d08--idempotency-keys-are-mandatory-and-a-replay-answers-200))
        this endpoint already gives an ordinary replay. A genuine cross-customer collision on
        `idempotency_key` alone (two different customers, same literal key string) still
        cannot happen here: two different customers never derive the same `order_id` to begin
        with, so this method never even sees that race — the database's own unique
        constraint on `idempotency_key` is what would catch that, at the caller.

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
                        str(order_id),
                        str(customer_id),
                        str(payload.restaurant_id),
                        total,
                        idempotency_key,
                    ),
                )
            except psycopg2.errors.UniqueViolation as exc:
                # Same order_id (a same-customer race, converged below) or the same literal
                # idempotency_key chosen by a different customer (a genuine collision, since
                # two different customers never derive the same order_id to race on).
                cur.execute(SELECT_BY_ID, (str(order_id),))
                existing = cur.fetchone()
                if existing is not None and str(existing["customer_id"]) == str(customer_id):
                    self._logger.info("Concurrent replay for order %s", order_id)
                    self._attach_items([existing])
                    return existing, False
                self._logger.info("Concurrent replay for key %s", idempotency_key)
                raise conflict(
                    "An order with this idempotency key is already being processed"
                ) from exc
            order = cur.fetchone()

            # Same transaction as the order itself: an order without its line items cannot
            # exist, the same guarantee D24 already gives the trail row below. Already have
            # `items_snapshot` in hand, so the row handed back to the caller is built from
            # it directly rather than re-read from what was just written.
            for line_no, item in enumerate(items_snapshot):
                cur.execute(
                    INSERT_LINE_ITEM,
                    {
                        "order_id": order["id"],
                        "line_no": line_no,
                        "menu_item_id": item["item_id"],
                        "item_name": item.get("name"),
                        "quantity": item["quantity"],
                        "unit_price": item["unit_price"],
                        "line_total": item["line_total"],
                        "customizations": Json(item.get("customizations")),
                    },
                )
                line_item_id = cur.fetchone()["id"]
                for option_position, option in enumerate(item.get("selected_options", [])):
                    cur.execute(
                        INSERT_LINE_ITEM_OPTION,
                        {
                            "line_item_id": line_item_id,
                            "group_key": option.get("group_id"),
                            "name": option["name"],
                            "extra_price": option.get("extra_price", 0.0),
                            "position": option_position,
                        },
                    )
            order["items"] = items_snapshot

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

            # No outbox row here any more (D53): `order.created` reaches Kafka via a
            # `publish_order_event_activity` the workflow's `create_order` Update handler
            # calls right after this method returns, which re-reads the row fresh over
            # `GET /orders/{id}/internal` — see orchestrator/activities/order.py.
            return order, True

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
        service: str | None = None,
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

        `service` overrides the identity this instance normally stamps on the trail row
        (`self._service_name`, fixed at construction). Needed because `apis/transitions.py`
        shares one `OrderRepository` instance across every caller that reaches it over
        HTTP — the orchestrator's worker among them — and `order_tracking_logs.service`
        must keep saying which *process* observed the transition, not which process
        happened to hold the connection that wrote it down (D36).
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
                    "service": service or self._service_name,
                    "updated_by": updated_by,
                    "raw_log": json.dumps(event or {"event": f"order_{new_status}"}),
                    "metadata": Json(metadata or {}),
                },
            )

            # No outbox row here any more (D53): `order.<new_status>` reaches Kafka via a
            # `publish_order_event_activity` the workflow calls right after this activity
            # returns — chained as a second, independently-retried Temporal activity rather
            # than a second write in this same transaction. `metadata` (the saga's
            # compensation reason on `order.cancelled`, say — D38: the orchestrator holds no
            # outbox of its own) travels with that second call instead of being written here.
            return updated, True

    def kitchen_queue(self, restaurant_id: UUID) -> list[dict]:
        """Orders awaiting this kitchen's decision, oldest first."""
        orders = self.all(SELECT_KITCHEN_QUEUE, (str(restaurant_id),))
        self._attach_items(orders)
        return orders

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
                # No outbox row here any more (D53): `order.kitchen.decided` reaches Kafka
                # via a `publish_order_event_activity` call the workflow's `kitchen_decision`
                # Update handler makes right after this write — see
                # orchestrator/workflows/order.py. `decided is not None` is still the guard
                # that keeps a second accept on an already-decided order from publishing
                # twice, the same role it played when the outbox write lived here directly.
                return decided, True
            cur.execute(SELECT_BY_ID, (str(order_id),))
            return cur.fetchone(), False

    def record_rider_report(self, order_id: UUID, stage: str) -> dict | None:
        """Record what the rider has told this service so far — a durable column, not an
        event (Week 3, D46). No outbox row and no trail entry: this is bookkeeping for the
        saga's own timeout recovery, the same category `kitchen_decision` already occupies,
        not a new fact anything downstream of Kafka needs to react to.

        Returns the updated row, or `None` if the guard rejected the write (already at this
        stage or past it) — the caller does not currently act on that, but the shape
        matches `decide_kitchen`'s for the same reason: a second report for a stage already
        recorded is a no-op, not an error, and the guard is what makes a retried signal
        relay safe to call twice.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(RECORD_RIDER_REPORT, {"order_id": str(order_id), "stage": stage})
            return cur.fetchone()
