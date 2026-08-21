"""PostgreSQL access for the `orders` table and the `order_tracking_logs` trail beside it.

Idempotency is enforced by a unique index on `idempotency_key`, so a replay that loses the
race to a concurrent submission is resolved here rather than by application-level locking.

`customer_id` and `restaurant_id` are plain UUID columns: they point into other services'
databases, where no foreign key can follow them, so main.py verifies both over HTTP before
calling in here. `customer_id` additionally never comes from the client — it is the subject
of the verified access token.

`order_tracking_logs.order_id` is the exception, and the reason the table was moved out of
MongoDB into this database: both ends live here, so the engine enforces it. An entry for an
order that does not exist is rejected, and the trail is deleted with the order it describes
rather than orphaned.
"""

import json
from decimal import Decimal
from logging import Logger
from uuid import UUID

import psycopg2
from psycopg2.extras import Json

from common.errors import conflict, unprocessable
from common.postgres import PostgresPool
from schemas import OrderCreateRequest, OrderTrackingLogCreateRequest

_COLUMNS = (
    "id, customer_id, restaurant_id, rider_id, items, total_amount, status, "
    "kitchen_decision, idempotency_key"
)

# What the kitchen is shown, and deliberately less than _COLUMNS. An admin deciding on an
# order needs to know what to cook; they have no business seeing what the customer paid or
# which idempotency key their client chose.
_KITCHEN_COLUMNS = "id, restaurant_id, items, status, created_at"

# "On the rail" — confirmed, and the kitchen has not answered yet. This one predicate is
# both the capacity count and the admin's queue, which is why the partial index in
# db/order/init.sql matches it exactly.
_ON_RAIL = "status = 'confirmed' AND kitchen_decision IS NULL"

_SELECT_BY_ID = f"SELECT {_COLUMNS} FROM orders WHERE id = %s"

_SELECT_BY_KEY = f"SELECT {_COLUMNS} FROM orders WHERE idempotency_key = %s"

_INSERT_ORDER = f"""
    INSERT INTO orders (customer_id, restaurant_id, items, total_amount, status, idempotency_key)
    VALUES (%s, %s, %s, %s, 'created', %s)
    RETURNING {_COLUMNS}
"""

# `old_status` is never accepted from a caller: it is read from the preceding entry so the
# chain cannot disagree with itself. The columns are aliased to the names the API has always
# used, which is what kept the migration off MongoDB invisible to clients.
_LOG_COLUMNS = (
    "id, order_id, old_status AS previous_status, new_status AS status, "
    "service, updated_by, raw_log, metadata, created_at"
)

_INSERT_LOG = f"""
    INSERT INTO order_tracking_logs
        (order_id, old_status, new_status, service, updated_by, raw_log, metadata)
    SELECT %(order_id)s::uuid,
           (SELECT prior.new_status
              FROM order_tracking_logs AS prior
             WHERE prior.order_id = %(order_id)s::uuid
             ORDER BY prior.seq DESC
             LIMIT 1),
           %(new_status)s::order_status,
           %(service)s,
           %(updated_by)s,
           %(raw_log)s,
           %(metadata)s::jsonb
    RETURNING {_LOG_COLUMNS}
"""

_SELECT_TIMELINE = f"""
    SELECT {_LOG_COLUMNS} FROM order_tracking_logs WHERE order_id = %s ORDER BY seq
"""

# Counts the rail this order is joining, deriving the restaurant from the order itself so
# the caller does not have to supply it — and so the count and the transition it gates stay
# one statement apart inside one transaction.
_COUNT_ON_RAIL_FOR_ORDER = f"""
    SELECT count(*) AS on_rail FROM orders
     WHERE restaurant_id = (SELECT restaurant_id FROM orders WHERE id = %(order_id)s::uuid)
       AND {_ON_RAIL}
"""

_SELECT_KITCHEN_QUEUE = f"""
    SELECT {_KITCHEN_COLUMNS} FROM orders
     WHERE restaurant_id = %s AND {_ON_RAIL}
     ORDER BY created_at
"""

# Guarded on both halves of "undecided": a second accept, or an accept racing a reject,
# changes nothing and the caller reports whichever decision actually stuck. The status
# clause also refuses a decision on an order that has already been cancelled out from
# under the kitchen — which is what a late click on a timed-out order is.
_DECIDE_KITCHEN = f"""
    UPDATE orders
       SET kitchen_decision = %(decision)s::kitchen_decision,
           kitchen_decided_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = %(order_id)s::uuid
       AND {_ON_RAIL}
    RETURNING {_COLUMNS}
"""

# A compare-and-set, not a blind UPDATE, and every clause earns its place.
#
# Temporal guarantees activities run *at least* once, so this statement is executed more
# than once for a single logical transition whenever a worker dies mid-activity or a
# response is lost. Three things follow from that:
#
#   `status <> new` makes a replay a no-op instead of a second identical transition, which
#   is what keeps the audit trail from growing an entry per retry.
#
#   `status NOT IN ('delivered','cancelled')` makes the terminal states final. A late
#   signal for an order that has already been cancelled cannot resurrect it.
#
#   The last clause only allows forward movement, except into 'cancelled', which is
#   reachable from anywhere still in flight. It leans on a property of the schema worth
#   knowing: a Postgres enum compares by *declaration order*, and `order_status` was
#   declared in lifecycle order, so `'delivered' > 'assigned'` is simply true. That is why
#   no separate ordering table is needed here.
#
# `rider_id` is COALESCEd so a later transition never clears an assignment made earlier.
_TRANSITION_ORDER = f"""
    UPDATE orders
       SET status = %(new_status)s::order_status,
           rider_id = COALESCE(%(rider_id)s::uuid, rider_id),
           updated_at = CURRENT_TIMESTAMP
     WHERE id = %(order_id)s::uuid
       AND status <> %(new_status)s::order_status
       AND status NOT IN ('delivered', 'cancelled')
       AND (
             %(new_status)s::order_status = 'cancelled'
             OR %(new_status)s::order_status > status
           )
    RETURNING {_COLUMNS}
"""


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


def _append_log(cur, entry: dict) -> dict:
    """Append one transition on an already-leased cursor, and return the row written.

    Takes a cursor rather than the pool so the caller decides the transaction: creating an
    order writes its first entry inside the same one, while a later transition arrives on
    its own.
    """
    cur.execute(_INSERT_LOG, entry)
    return cur.fetchone()


class OrderRepository:
    def __init__(self, db: PostgresPool, *, logger: Logger, service_name: str):
        self._db = db
        self._logger = logger
        # Recorded on the entries this service writes, so a trail read back later says
        # which service observed each transition.
        self._service_name = service_name

    def find(self, order_id: UUID) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_BY_ID, (str(order_id),))
            return cur.fetchone()

    def find_by_idempotency_key(self, key: str) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_BY_KEY, (key,))
            return cur.fetchone()

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
        than the request body — see main.create_order.

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
                    _INSERT_ORDER,
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

            _append_log(
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
                cur.execute(_COUNT_ON_RAIL_FOR_ORDER, {"order_id": str(order_id)})
                on_rail = cur.fetchone()["on_rail"]
                if on_rail >= capacity_limit:
                    raise AtCapacity(order_id, on_rail, capacity_limit)

            cur.execute(
                _TRANSITION_ORDER,
                {
                    "order_id": str(order_id),
                    "new_status": new_status,
                    "rider_id": str(rider_id) if rider_id else None,
                },
            )
            updated = cur.fetchone()

            if updated is None:
                cur.execute(_SELECT_BY_ID, (str(order_id),))
                current = cur.fetchone()
                if current is not None:
                    self._logger.info(
                        "Order %s is %s; transition to %s is a no-op",
                        order_id,
                        current["status"],
                        new_status,
                    )
                return current, False

            _append_log(
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
        with self._db.cursor() as cur:
            cur.execute(_SELECT_KITCHEN_QUEUE, (str(restaurant_id),))
            return cur.fetchall()

    def decide_kitchen(self, order_id: UUID, decision: str) -> tuple[dict | None, bool]:
        """Record the kitchen's accept or reject.

        Returns `(order, changed)`. `changed` is False when the order was already decided,
        or is no longer `confirmed` — which lets the caller signal the saga exactly once. A
        second accept must not tell the workflow twice, and a click on an order the saga
        already timed out and cancelled must not un-cancel it.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                _DECIDE_KITCHEN, {"order_id": str(order_id), "decision": decision}
            )
            decided = cur.fetchone()
            if decided is not None:
                return decided, True
            cur.execute(_SELECT_BY_ID, (str(order_id),))
            return cur.fetchone(), False


class OrderTrackingRepository:
    """The append-only trail. Nothing here updates or deletes an entry."""

    def __init__(self, db: PostgresPool):
        self._db = db

    def append(self, payload: OrderTrackingLogCreateRequest) -> dict:
        """Record a transition reported by a sibling service.

        Both rejections below come from the engine rather than from a check written here,
        which is the point of the move: the foreign key knows which orders exist, and the
        `order_status` enum knows which statuses do.
        """
        with self._db.cursor(commit=True) as cur:
            try:
                return _append_log(
                    cur,
                    {
                        "order_id": str(payload.order_id),
                        "new_status": payload.status,
                        "service": payload.service,
                        "updated_by": payload.updated_by,
                        "raw_log": payload.raw_log,
                        "metadata": Json(payload.metadata or {}),
                    },
                )
            except psycopg2.errors.ForeignKeyViolation as exc:
                raise unprocessable(
                    f"Unknown order {payload.order_id}; there is nothing to log against"
                ) from exc
            except psycopg2.errors.InvalidTextRepresentation as exc:
                raise unprocessable(
                    f"'{payload.status}' is not an order status"
                ) from exc

    def timeline(self, order_id: UUID) -> list[dict]:
        """Every transition for one order, oldest first."""
        with self._db.cursor() as cur:
            cur.execute(_SELECT_TIMELINE, (str(order_id),))
            return cur.fetchall()
