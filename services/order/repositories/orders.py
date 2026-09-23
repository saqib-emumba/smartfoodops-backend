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
from common.outbox import append_outbox
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

            # Same transaction as the order itself: an order without its line items cannot
            # exist, the same guarantee D24/D39 already give the trail row and the outbox
            # row below. Already have `items_snapshot` in hand, so the row handed back to
            # the caller is built from it directly rather than re-read from what was just
            # written.
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

            # Same transaction as the insert and the trail row above (D39) — an order
            # without an `order.created` outbox row cannot exist, the same guarantee D24
            # already gives order_tracking_logs. Items are left out of the payload
            # deliberately: they can be large, and a consumer that needs them can read
            # GET /api/v1/orders/{id} — the outbox carries the facts a consumer reacts to,
            # not a full copy of the row.
            append_outbox(
                cur,
                table="order_outbox",
                aggregate_type="order",
                aggregate_id=order["id"],
                event_type="order.created",
                payload={
                    "order_id": str(order["id"]),
                    "customer_id": str(order["customer_id"]),
                    "restaurant_id": str(order["restaurant_id"]),
                    "total_amount": order["total_amount"],
                    "status": order["status"],
                    "items_count": len(order["items"]),
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

            # Gated on the exact same branch as the trail row above — the CAS above this
            # block already guarantees `changed` (this branch) fires once per logical
            # transition regardless of how many times Temporal retries the activity, so
            # this inherits that guarantee for free rather than re-deriving it (D39).
            # `new_status` names the event type directly: order.confirmed, order.assigned,
            # order.picked_up, order.delivered, order.cancelled (D40) — the last of those
            # carries the saga's compensation reason via `metadata`, which is how a reason
            # code recorded nowhere the orchestrator can write (it holds no outbox of its
            # own, D38) still reaches Kafka.
            append_outbox(
                cur,
                table="order_outbox",
                aggregate_type="order",
                aggregate_id=order_id,
                event_type=f"order.{new_status}",
                payload={
                    "order_id": str(order_id),
                    "new_status": new_status,
                    "customer_id": str(updated["customer_id"]),
                    "restaurant_id": str(updated["restaurant_id"]),
                    "rider_id": str(updated["rider_id"]) if updated["rider_id"] else None,
                    "total_amount": updated["total_amount"],
                    "metadata": metadata or {},
                },
            )
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
                # No order_tracking_logs row exists for this write (DECIDE_KITCHEN never
                # called append_log — see this method's own docstring), so the outbox is
                # the only record of a kitchen decision anywhere outside `orders` itself.
                # Guarded on `decided is not None` exactly like the trail row would be if
                # one existed, for the same reason: a second accept on an already-decided
                # order must not emit twice.
                append_outbox(
                    cur,
                    table="order_outbox",
                    aggregate_type="order",
                    aggregate_id=order_id,
                    event_type="order.kitchen.decided",
                    payload={
                        "order_id": str(order_id),
                        "decision": decision,
                        "restaurant_id": str(decided["restaurant_id"]),
                    },
                )
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
