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

Every *request-path* lookup here runs as the customer who placed the order, by forwarding
their bearer token, so this service can never read more than they could.

The saga's clients at the bottom of this file are the exception, and the exception is the
point. An activity has no user behind it: a bearer token would be written into durable,
UI-visible workflow history and would expire long before a saga that waits on a kitchen
finishes. Those calls carry the internal key instead (D26).

There is no saga client for the Restaurant Service, and that is the whole shape of D32. The
saga used to call it four times — ticket, read-back, expiry and coordinates — and now calls
it zero: the kitchen's decision is a column on `orders`, and the restaurant's capacity and
coordinates ride in the workflow payload, captured once at checkout from a lookup this
service already performed.
"""

import os
from logging import Logger
from uuid import UUID

from common.auth import bearer, internal_headers
from common.config import (
    DEFAULT_MENU_SERVICE_URL,
    DEFAULT_PAYMENT_SERVICE_URL,
    DEFAULT_RESTAURANT_SERVICE_URL,
    DEFAULT_RIDER_SERVICE_URL,
    DEFAULT_USER_SERVICE_URL,
    PAYMENT_HTTP_TIMEOUT,
)
from common.errors import forbidden, unprocessable
from common.service_client import ServiceClient

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", DEFAULT_USER_SERVICE_URL)
RESTAURANT_SERVICE_URL = os.getenv(
    "RESTAURANT_SERVICE_URL", DEFAULT_RESTAURANT_SERVICE_URL
)
MENU_SERVICE_URL = os.getenv("MENU_SERVICE_URL", DEFAULT_MENU_SERVICE_URL)
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", DEFAULT_PAYMENT_SERVICE_URL)
RIDER_SERVICE_URL = os.getenv("RIDER_SERVICE_URL", DEFAULT_RIDER_SERVICE_URL)


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

    def verify_owner(self, restaurant_id: UUID, owner_id: UUID, token: str) -> dict:
        """Confirm this caller owns the restaurant an order was placed with.

        Who owns a restaurant is the Restaurant Service's fact, so it is resolved there
        rather than duplicated here (D16) — this reads the record as the caller and compares
        the owner, exactly as the Menu Service does before letting an admin publish a menu.

        Forwards the admin's own bearer token rather than the internal key: the caller *is*
        a user, so the call should be able to do no more than they could (D15).
        """
        restaurant = self._client.get(
            f"/api/v1/restaurants/{restaurant_id}",
            missing=f"Restaurant {restaurant_id} no longer exists",
            unreachable_hint="cannot verify who owns this restaurant",
            bad_gateway_hint="verifying restaurant ownership",
            headers=bearer(token),
        )
        if str(restaurant.get("owner_id")) != str(owner_id):
            raise forbidden(
                f"You do not own restaurant {restaurant_id}, so you may not decide its orders"
            )
        return restaurant

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


# --- Saga clients -------------------------------------------------------------------------
#
# Used only by activities.py, and only on the internal key. Each returns the sibling's raw
# response so the activity — not the transport — decides what a business outcome means.


class SagaPaymentClient:
    def __init__(self, logger: Logger):
        # The only client here with a non-default timeout. Authorising a card is a round
        # trip to an external processor and takes seconds, so the platform-wide 5s
        # HTTP_TIMEOUT would abort a call that was going to succeed.
        self._client = ServiceClient(
            "Payment Service",
            PAYMENT_SERVICE_URL,
            logger=logger,
            timeout=PAYMENT_HTTP_TIMEOUT,
        )

    def authorize(self, order_id: UUID, amount: str, idempotency_key: str) -> dict:
        return self._client.post(
            "/api/v1/payments/authorize",
            json={
                "order_id": str(order_id),
                # A string, so an exact decimal survives the JSON boundary (D07).
                "amount": amount,
                "idempotency_key": idempotency_key,
            },
            missing=f"Order {order_id} is unknown to the Payment Service",
            missing_error=unprocessable,
            unreachable_hint="cannot authorise the payment",
            bad_gateway_hint="authorising the payment",
            headers=internal_headers(),
        )

    def refund(self, order_id: UUID, reason: str) -> dict:
        return self._client.post(
            "/api/v1/payments/refund",
            json={"order_id": str(order_id), "reason": reason},
            missing=f"Order {order_id} has no payment to refund",
            missing_error=unprocessable,
            unreachable_hint="cannot refund the payment",
            bad_gateway_hint="refunding the payment",
            headers=internal_headers(),
        )


class SagaRiderClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient("Rider Service", RIDER_SERVICE_URL, logger=logger)

    def dispatch(self, order_id: UUID, latitude: float, longitude: float) -> dict:
        return self._client.post(
            "/api/v1/riders/dispatch",
            json={
                "order_id": str(order_id),
                "restaurant_latitude": latitude,
                "restaurant_longitude": longitude,
            },
            missing=f"Order {order_id} cannot be dispatched",
            missing_error=unprocessable,
            unreachable_hint="cannot dispatch a rider",
            bad_gateway_hint="dispatching a rider",
            headers=internal_headers(),
        )

    def release(self, order_id: UUID) -> dict:
        return self._client.post(
            "/api/v1/riders/release",
            json={"order_id": str(order_id)},
            missing=f"Order {order_id} has no rider to release",
            missing_error=unprocessable,
            unreachable_hint="cannot release the rider",
            bad_gateway_hint="releasing the rider",
            headers=internal_headers(),
        )
