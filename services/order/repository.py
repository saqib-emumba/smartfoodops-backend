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
    "idempotency_key"
)

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
        """
        with self._db.cursor(commit=True) as cur:
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
