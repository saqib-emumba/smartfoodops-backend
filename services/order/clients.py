"""Outbound calls to sibling services.

An order references three things this service's database does not hold: a customer, a
restaurant and a menu. Under database-per-service there is no foreign key that could span
those databases, so each reference is resolved over HTTP instead of by the engine — the
customer against the User Service, the restaurant against the Restaurant Service, and
pricing against the Menu Service.

The audit trail used to be a fourth call, into the Menu Service's MongoDB. It is not here
any more: `order_tracking_logs` moved into this service's own database, so the first entry
is written in the same transaction as the order rather than posted over the network after
it — see repository.OrderRepository.create.

An unknown customer or restaurant surfaces as 422 rather than 404, which is what the
foreign-key violation these checks replace already returned: the request is well formed and
the order is not the thing that is missing — the entity it points at is.

Every lookup here runs as the customer who placed the order, by forwarding their bearer
token, so this service can never read more than they could.
"""

import os
from logging import Logger
from uuid import UUID

from common.auth import bearer
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

    def verify_customer(self, customer_id: UUID, token: str) -> dict:
        """Confirm the customer exists — the check the `customer_id` foreign key made.

        `customer_id` now comes from the token being forwarded, so this is a self-read and
        satisfies the User Service's own self-or-admin rule.
        """
        return self._client.get(
            f"/api/v1/users/{customer_id}",
            missing=f"Unknown customer {customer_id} referenced by this order",
            missing_error=unprocessable,
            unreachable_hint="cannot verify the customer",
            bad_gateway_hint="verifying the customer",
            headers=bearer(token),
        )


class RestaurantServiceClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient(
            "Restaurant Service", RESTAURANT_SERVICE_URL, logger=logger
        )

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def verify_restaurant(self, restaurant_id: UUID, token: str) -> dict:
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
            headers=bearer(token),
        )


class MenuServiceClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient("Menu Service", MENU_SERVICE_URL, logger=logger)

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def fetch_menu(self, restaurant_id: UUID, token: str) -> dict:
        """Pull the restaurant's published menu, the source of truth for pricing."""
        return self._client.get(
            f"/api/v1/menus/{restaurant_id}",
            missing=f"No active menu found for restaurant {restaurant_id}",
            unreachable_hint="cannot validate the order",
            headers=bearer(token),
        )
