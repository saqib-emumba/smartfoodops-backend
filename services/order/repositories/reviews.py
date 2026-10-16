"""`order_reviews`: one rating and comment per delivered order (D64)."""

from uuid import UUID

import psycopg2

from common.errors import conflict
from common.repository import Repository

_COLUMNS = "id, order_id, restaurant_id, rating, comment, created_at"

_INSERT = f"""
    INSERT INTO order_reviews (order_id, customer_id, restaurant_id, rating, comment)
    VALUES (%(order_id)s, %(customer_id)s, %(restaurant_id)s, %(rating)s, %(comment)s)
    RETURNING {_COLUMNS}
"""

_AGGREGATE = """
    SELECT COUNT(*) AS review_count, AVG(rating)::float AS average_rating
      FROM order_reviews WHERE restaurant_id = %s
"""

# Commented reviews only: a bare star rating gives a summariser nothing to quote.
_RECENT_COMMENTED = """
    SELECT rating, comment, created_at
      FROM order_reviews
     WHERE restaurant_id = %s AND comment IS NOT NULL AND comment <> ''
     ORDER BY created_at DESC
     LIMIT %s
"""


class OrderReviewRepository(Repository):
    def create(self, *, order: dict, rating: int, comment: str | None) -> dict:
        """Record the review. A second review of the same order is a `409`, decided by the
        UNIQUE constraint rather than a read-then-write that two submits could both pass."""
        with self._db.cursor(commit=True) as cur:
            try:
                cur.execute(
                    _INSERT,
                    {
                        "order_id": str(order["id"]),
                        "customer_id": str(order["customer_id"]),
                        "restaurant_id": str(order["restaurant_id"]),
                        "rating": rating,
                        "comment": comment or None,
                    },
                )
            except psycopg2.errors.UniqueViolation as exc:
                raise conflict(f"Order {order['id']} has already been reviewed") from exc
            return cur.fetchone()

    def for_restaurant(self, restaurant_id: UUID, limit: int) -> dict:
        aggregate = self.one(_AGGREGATE, (str(restaurant_id),))
        reviews = self.all(_RECENT_COMMENTED, (str(restaurant_id), limit))
        return {
            "restaurant_id": restaurant_id,
            "review_count": aggregate["review_count"],
            "average_rating": round(aggregate["average_rating"], 2)
            if aggregate["average_rating"] is not None
            else None,
            "reviews": reviews,
        }
