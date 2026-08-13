"""Outbound calls to sibling services.

An order references three things this service's database does not hold: a customer, a
restaurant and a menu. Under database-per-service there is no foreign key that could span
those databases, so each reference is resolved over HTTP instead of by the engine — the
customer against the User Service, the restaurant against the Restaurant Service, and
pricing plus the audit trail against the Menu Service.

An unknown customer or restaurant surfaces as 422 rather than 404, which is what the
foreign-key violation these checks replace already returned: the request is well formed and
the order is not the thing that is missing — the entity it points at is.
"""

import json
import os
from logging import Logger
from uuid import UUID

from common.config import (
    DEFAULT_MENU_SERVICE_URL,
    DEFAULT_RESTAURANT_SERVICE_URL,
    DEFAULT_USER_SERVICE_URL,
)
from common.errors import unprocessable
from common.service_client import ServiceClient

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", DEFAULT_USER_SERVICE_URL)
RESTAURANT_SERVICE_URL = os.getenv(
    "RESTAURANT_SERVICE_URL", DEFAULT_RESTAURANT_SERVICE_URL
)
MENU_SERVICE_URL = os.getenv("MENU_SERVICE_URL", DEFAULT_MENU_SERVICE_URL)


class UserServiceClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient("User Service", USER_SERVICE_URL, logger=logger)

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def verify_customer(self, customer_id: UUID) -> dict:
        """Confirm the customer exists — the check the `customer_id` foreign key made."""
        return self._client.get(
            f"/api/v1/users/{customer_id}",
            missing=f"Unknown customer {customer_id} referenced by this order",
            missing_error=unprocessable,
            unreachable_hint="cannot verify the customer",
            bad_gateway_hint="verifying the customer",
        )


class RestaurantServiceClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient(
            "Restaurant Service", RESTAURANT_SERVICE_URL, logger=logger
        )

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def verify_restaurant(self, restaurant_id: UUID) -> dict:
        """Confirm the restaurant exists — the check the `restaurant_id` foreign key made.

        Existence only: whether a restaurant may currently take orders is the Menu
        Service's call, and an unpublished or withdrawn menu already fails the re-pricing
        step that runs before this check.
        """
        return self._client.get(
            f"/api/v1/restaurants/{restaurant_id}",
            missing=f"Unknown restaurant {restaurant_id} referenced by this order",
            missing_error=unprocessable,
            unreachable_hint="cannot verify the restaurant",
            bad_gateway_hint="verifying the restaurant",
        )


class MenuServiceClient:
    def __init__(self, logger: Logger, *, service_name: str):
        self._client = ServiceClient("Menu Service", MENU_SERVICE_URL, logger=logger)
        self._service_name = service_name

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def fetch_menu(self, restaurant_id: UUID) -> dict:
        """Pull the restaurant's published menu, the source of truth for pricing."""
        return self._client.get(
            f"/api/v1/menus/{restaurant_id}",
            missing=f"No active menu found for restaurant {restaurant_id}",
            unreachable_hint="cannot validate the order",
        )

    def write_audit_log(self, order: dict, idempotency_key: str) -> bool:
        """Record the 'created' transition through the Menu Service logging endpoint.

        Best-effort: the order is already committed, so a logging failure is reported but
        does not fail the client's request.
        """
        raw_log = json.dumps(
            {
                "event": "order_created",
                "order_id": str(order["id"]),
                "total_amount": float(order["total_amount"]),
                "items_count": len(order["items"]),
            }
        )
        body = {
            "order_id": str(order["id"]),
            "status": "created",
            "service": self._service_name,
            "raw_log": raw_log,
            "updated_by": "customer_client",
            "metadata": {"idempotency_key": idempotency_key},
        }
        return self._client.post_best_effort(
            "/api/v1/menus/logs", body, purpose="audit log"
        )
