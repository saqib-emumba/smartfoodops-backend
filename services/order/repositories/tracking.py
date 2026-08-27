"""The append-only `order_tracking_logs` trail.

`order_tracking_logs.order_id` is a real foreign key, unlike the plain UUIDs `orders` holds
for the customer and restaurant — both ends of this one live in this database, so the engine
enforces it. An entry for an order that does not exist is rejected, and the trail is deleted
with the order it describes rather than orphaned.
"""

from uuid import UUID

import psycopg2
from psycopg2.extras import Json

from common.errors import unprocessable
from common.repository import Repository
from order.repositories.sql import SELECT_TIMELINE, append_log
from order.schemas.tracking import OrderTrackingLogCreateRequest


class OrderTrackingRepository(Repository):
    """The append-only trail. Nothing here updates or deletes an entry."""

    def append(self, payload: OrderTrackingLogCreateRequest) -> dict:
        """Record a transition reported by a sibling service.

        Both rejections below come from the engine rather than from a check written here,
        which is the point of the move: the foreign key knows which orders exist, and the
        `order_status` enum knows which statuses do.
        """
        with self._db.cursor(commit=True) as cur:
            try:
                return append_log(
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
        return self.all(SELECT_TIMELINE, (str(order_id),))
