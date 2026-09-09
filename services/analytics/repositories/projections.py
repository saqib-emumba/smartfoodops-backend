"""PostgreSQL access for `processed_events` and `order_projections` — the consumer's dedup
gate and the business-facing state it derives (Week 3, D40).

Both tables are always written together, in one transaction, from `apply_event`: an event
whose fact does not commit and whose dedup row does not commit are the same failure, or
neither happens — this is the mirror image of the outbox pattern the producing services
use (D39), applied to a consumer instead of a producer. Without it, a crash between the two
writes could mark an event processed without its effect landing, or apply an effect twice
because the dedup row never committed.
"""

from decimal import Decimal
from uuid import UUID

from common.repository import Repository

_CONSUMER_GROUP = "analytics"

_MARK_PROCESSED = """
    INSERT INTO processed_events (consumer_group, event_id, event_type)
    VALUES (%(consumer_group)s, %(event_id)s, %(event_type)s)
    ON CONFLICT (consumer_group, event_id) DO NOTHING
    RETURNING event_id
"""

_UPSERT_ORDER_CREATED = """
    INSERT INTO order_projections
        (order_id, restaurant_id, customer_id, total_amount, status, placed_at)
    VALUES (%(order_id)s, %(restaurant_id)s, %(customer_id)s, %(total_amount)s,
            'created', %(occurred_at)s)
    ON CONFLICT (order_id) DO NOTHING
"""

_UPDATE_ORDER_STATUS = """
    UPDATE order_projections
       SET status = %(status)s,
           updated_at = CURRENT_TIMESTAMP
     WHERE order_id = %(order_id)s
"""

_UPDATE_ORDER_DELIVERED = """
    UPDATE order_projections
       SET status = 'delivered',
           delivered_at = %(occurred_at)s,
           delivery_seconds = EXTRACT(EPOCH FROM (%(occurred_at)s::timestamptz - placed_at)),
           updated_at = CURRENT_TIMESTAMP
     WHERE order_id = %(order_id)s
"""

_UPDATE_ORDER_CANCELLED = """
    UPDATE order_projections
       SET status = 'cancelled',
           cancelled_at = %(occurred_at)s,
           updated_at = CURRENT_TIMESTAMP
     WHERE order_id = %(order_id)s
"""

# Historical totals, queried once at startup to seed the process-local Prometheus Counters
# — see consumer.py's own comment on why a Counter needs this and a Gauge would not.
_COUNT_BY_STATUS = "SELECT status, count(*) AS n FROM order_projections GROUP BY status"
_AVG_DELIVERY_SECONDS = (
    "SELECT avg(delivery_seconds) AS avg_seconds FROM order_projections "
    "WHERE delivery_seconds IS NOT NULL"
)


class ProjectionsRepository(Repository):
    def already_processed(self) -> None:
        """No-op placeholder retained for readability at call sites — dedup is enforced by
        `mark_processed`'s `ON CONFLICT DO NOTHING` inside the same transaction as the
        effect, not by a separate pre-check (a pre-check-then-write is exactly the race a
        unique constraint exists to close)."""

    def apply_order_created(self, *, event_id: UUID, data: dict, occurred_at) -> bool:
        return self._apply(
            event_id=event_id,
            event_type="order.created",
            statement=_UPSERT_ORDER_CREATED,
            params={
                "order_id": data["order_id"],
                "restaurant_id": data.get("restaurant_id"),
                "customer_id": data.get("customer_id"),
                "total_amount": data.get("total_amount"),
                "occurred_at": occurred_at,
            },
        )

    def apply_order_status(self, *, event_id: UUID, event_type: str, status: str, data: dict) -> bool:
        return self._apply(
            event_id=event_id,
            event_type=event_type,
            statement=_UPDATE_ORDER_STATUS,
            params={"order_id": data["order_id"], "status": status},
        )

    def apply_order_delivered(self, *, event_id: UUID, data: dict, occurred_at) -> bool:
        return self._apply(
            event_id=event_id,
            event_type="order.delivered",
            statement=_UPDATE_ORDER_DELIVERED,
            params={"order_id": data["order_id"], "occurred_at": occurred_at},
        )

    def apply_order_cancelled(self, *, event_id: UUID, data: dict, occurred_at) -> bool:
        return self._apply(
            event_id=event_id,
            event_type="order.cancelled",
            statement=_UPDATE_ORDER_CANCELLED,
            params={"order_id": data["order_id"], "occurred_at": occurred_at},
        )

    def mark_seen(self, *, event_id: UUID, event_type: str) -> bool:
        """Dedup-record an event with no projection effect of its own — `payment.*` and
        `order.kitchen.decided` are schema-validated and offset-committed like every other
        event, but this table has no column any of them would update. Returns whether the
        event was new, for symmetry with the `apply_*` methods, though no caller currently
        acts on it."""
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                _MARK_PROCESSED,
                {"consumer_group": _CONSUMER_GROUP, "event_id": str(event_id), "event_type": event_type},
            )
            return cur.fetchone() is not None

    def _apply(self, *, event_id: UUID, event_type: str, statement: str, params: dict) -> bool:
        """Mark processed and apply the effect in one transaction.

        Returns whether the event was NEW (True) or already applied (False) — the caller
        uses this to decide whether to advance the in-memory Counters, so a redelivered
        message (Kafka is at-least-once) bumps nothing a second time.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                _MARK_PROCESSED,
                {"consumer_group": _CONSUMER_GROUP, "event_id": str(event_id), "event_type": event_type},
            )
            if cur.fetchone() is None:
                return False  # ON CONFLICT matched nothing new: already processed
            cur.execute(statement, params)
            return True

    def status_counts(self) -> dict[str, int]:
        rows = self.all(_COUNT_BY_STATUS)
        return {row["status"]: row["n"] for row in rows}

    def average_delivery_seconds(self) -> float | None:
        row = self.one(_AVG_DELIVERY_SECONDS)
        value = row["avg_seconds"] if row else None
        return float(value) if value is not None else None
