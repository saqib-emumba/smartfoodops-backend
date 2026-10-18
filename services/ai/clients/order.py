"""The Order Service, as the AI layer reads it (internal routes, D64).

Only internal-key routes: the AI Service holds the caller's identity and decides ownership
itself, so it asks the Order Service for facts rather than asking it to authorise.
"""

from fastapi import HTTPException

from common.auth import internal_headers
from common.config import DEFAULT_ORDER_SERVICE_URL
from common.service_client import ServiceFacade


class OrderServiceClient(ServiceFacade):
    display_name = "Order Service"
    env_var = "ORDER_SERVICE_URL"
    default_url = DEFAULT_ORDER_SERVICE_URL

    def explain_context(self, order_id: str) -> dict:
        """The order, its reduced transition trail and its running saga's progress."""
        return self._client.get(
            f"/api/v1/orders/internal/{order_id}/explain-context",
            missing=f"Order {order_id} not found",
            unreachable_hint="cannot read the order",
            headers=internal_headers(),
        )

    def latest_order_id(self, customer_id: str, status: str | None = None) -> str | None:
        """The customer's most recent order id (optionally the latest cancelled one), or None."""
        try:
            body = self._client.get(
                f"/api/v1/orders/internal/customers/{customer_id}/latest-order"
                + (f"?status={status}" if status else ""),
                missing=f"Customer {customer_id} has no orders",
                unreachable_hint="cannot look up the latest order",
                headers=internal_headers(),
            )
        except HTTPException as exc:
            if exc.status_code == 404:  # the only status that means "never ordered"
                return None
            raise
        return body["order_id"]

    def customer_summary(self, customer_id: str, limit: int = 5) -> dict:
        """`{counts: {total, in_progress, delivered, cancelled, by_status}, recent: [...]}` for one customer."""
        return self._client.get(
            f"/api/v1/orders/internal/customers/{customer_id}/summary?limit={limit}",
            missing=f"Customer {customer_id} has no orders",
            unreachable_hint="cannot read the customer's orders",
            headers=internal_headers(),
        )

    def restaurant_reviews(self, restaurant_id: str, limit: int = 30) -> dict:
        """`{review_count, average_rating, reviews[{rating, comment, created_at}]}`."""
        return self._client.get(
            f"/api/v1/orders/internal/restaurants/{restaurant_id}/reviews?limit={limit}",
            missing=f"Restaurant {restaurant_id} has no reviews",
            unreachable_hint="cannot read reviews",
            headers=internal_headers(),
        )
