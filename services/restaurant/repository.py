"""PostgreSQL access for the `restaurants` table and the `order_tickets` queue beside it.

`owner_id` is a plain UUID column pointing into the User Service's database, so no foreign
key can validate it here. main.py verifies the owner over HTTP before calling in.

`order_tickets.restaurant_id` is the opposite case, and the reason the queue lives in this
database rather than the Order Service's: both ends are here, so the engine enforces it.
`order_id` points into `sfo_order_core` and is therefore a plain UUID — but it is UNIQUE,
which is what makes a retried "send this ticket" activity a no-op instead of a duplicate.
"""

from uuid import UUID

import psycopg2
from psycopg2.extras import Json

from common.postgres import PostgresPool
from schemas import RestaurantOnboardRequest

_COLUMNS = "id, owner_id, name, address, latitude, longitude, is_active, capacity"

_INSERT_RESTAURANT = f"""
    INSERT INTO restaurants (owner_id, name, address, latitude, longitude, capacity)
    VALUES (%s, %s, %s, %s, %s, %s)
    RETURNING {_COLUMNS}
"""

_SELECT_RESTAURANT = f"SELECT {_COLUMNS} FROM restaurants WHERE id = %s"

_TICKET_COLUMNS = (
    "id, order_id, restaurant_id, items, status, decided_at, created_at"
)

# Counts against `restaurants.capacity`. Only `pending` tickets occupy the kitchen — an
# accepted order is being cooked and a rejected one was never started.
_COUNT_PENDING = """
    SELECT count(*) AS pending FROM order_tickets
     WHERE restaurant_id = %s AND status = 'pending'
"""

# ON CONFLICT DO NOTHING rather than an upsert: a resent ticket must not reset a decision
# the kitchen has already made. The caller reads the existing row when this returns nothing.
_INSERT_TICKET = f"""
    INSERT INTO order_tickets (order_id, restaurant_id, items)
    VALUES (%s, %s, %s)
    ON CONFLICT (order_id) DO NOTHING
    RETURNING {_TICKET_COLUMNS}
"""

_SELECT_TICKET = f"SELECT {_TICKET_COLUMNS} FROM order_tickets WHERE order_id = %s"

_SELECT_QUEUE = f"""
    SELECT {_TICKET_COLUMNS} FROM order_tickets
     WHERE restaurant_id = %s AND status = %s
     ORDER BY created_at
"""

# Guarded on `pending`, so a second accept — or an accept racing a reject — changes nothing
# and the caller returns the decision that actually stuck. Also what stops a saga's
# expiry from overwriting a decision the kitchen made a moment earlier.
_DECIDE_TICKET = f"""
    UPDATE order_tickets
       SET status = %s::ticket_status,
           decided_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE order_id = %s
       AND status = 'pending'
    RETURNING {_TICKET_COLUMNS}
"""


class RestaurantRepository:
    def __init__(self, db: PostgresPool):
        self._db = db

    def onboard(self, payload: RestaurantOnboardRequest, owner_id: UUID) -> dict:
        """Insert a restaurant. `owner_id` is passed separately because it comes from the
        access token rather than the request body — see main.onboard_restaurant."""
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                _INSERT_RESTAURANT,
                (
                    str(owner_id),
                    payload.name,
                    payload.address,
                    payload.latitude,
                    payload.longitude,
                    payload.capacity,
                ),
            )
            return cur.fetchone()

    def find(self, restaurant_id: UUID) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_RESTAURANT, (str(restaurant_id),))
            return cur.fetchone()


class TicketRepository:
    """The kitchen queue: one ticket per order, and the kitchen's answer to it."""

    def __init__(self, db: PostgresPool):
        self._db = db

    def find(self, order_id: UUID) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_TICKET, (str(order_id),))
            return cur.fetchone()

    def queue(self, restaurant_id: UUID, status: str = "pending") -> list[dict]:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_QUEUE, (str(restaurant_id), status))
            return cur.fetchall()

    def enqueue(
        self, order_id: UUID, restaurant_id: UUID, items: list[dict], capacity: int
    ) -> tuple[dict | None, str]:
        """Queue an order for a kitchen, or explain why not.

        Returns `(ticket, outcome)` where outcome is one of `queued`, `already_queued` or
        `at_capacity`. Three outcomes rather than an exception for the last one, because
        being full is a business answer the saga compensates for — raising would make the
        workflow retry a condition that a retry cannot change.

        The count and the insert share a transaction, so two simultaneous orders cannot
        both see the last free slot.
        """
        with self._db.cursor(commit=True) as cur:
            # An already-queued order short-circuits before the capacity check: a retried
            # activity must not be refused for capacity it is already occupying.
            cur.execute(_SELECT_TICKET, (str(order_id),))
            existing = cur.fetchone()
            if existing is not None:
                return existing, "already_queued"

            cur.execute(_COUNT_PENDING, (str(restaurant_id),))
            if cur.fetchone()["pending"] >= capacity:
                return None, "at_capacity"

            try:
                cur.execute(
                    _INSERT_TICKET,
                    (str(order_id), str(restaurant_id), Json(items)),
                )
            except psycopg2.errors.ForeignKeyViolation as exc:
                # The restaurant vanished between the caller's lookup and this insert.
                # The foreign key is the only thing that could notice, which is why the
                # queue lives in this database rather than the Order Service's.
                raise exc
            inserted = cur.fetchone()
            if inserted is None:
                # Lost the race on ON CONFLICT; whoever won holds the ticket.
                cur.execute(_SELECT_TICKET, (str(order_id),))
                return cur.fetchone(), "already_queued"
            return inserted, "queued"

    def expire(self, order_id: UUID) -> dict | None:
        """Retire a ticket whose order is no longer going to happen.

        Only `pending` tickets are touched, so this cannot overwrite a decision the kitchen
        made — and it is idempotent, which a compensating action has to be.

        This exists because `status = 'pending'` is what the capacity count filters on. A
        ticket left pending by a cancelled order would occupy a slot in that kitchen's
        queue permanently, and enough of them would make the restaurant unable to accept
        anything. `ticket_status` has had an `expired` member since the table was created;
        this is what finally sets it.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(_DECIDE_TICKET, ("expired", str(order_id)))
            return cur.fetchone()

    def decide(self, order_id: UUID, decision: str) -> tuple[dict | None, bool]:
        """Record the kitchen's accept or reject.

        Returns `(ticket, changed)`. `changed` is False when the ticket was already
        decided, which lets the caller relay a signal exactly once — a second accept must
        not tell the workflow twice.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(_DECIDE_TICKET, (decision, str(order_id)))
            decided = cur.fetchone()
            if decided is not None:
                return decided, True
            cur.execute(_SELECT_TICKET, (str(order_id),))
            return cur.fetchone(), False
