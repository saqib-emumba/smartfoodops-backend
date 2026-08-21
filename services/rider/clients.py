"""Outbound calls to sibling services.

Two different credentials are used here, and the difference is the point:

* The **User Service** lookup runs as the rider, forwarding their bearer token, so this
  service can never read more about an account than the account holder could (D15).
* The **Order Service** signal relay runs on the internal key. A pickup is a fact this
  service observed, and the workflow it feeds is not something an end user may poke — the
  same reasoning that keeps the audit trail internal-only.
"""

import os
from logging import Logger
from uuid import UUID

from common.auth import bearer, internal_headers
from common.config import DEFAULT_ORDER_SERVICE_URL, DEFAULT_USER_SERVICE_URL
from common.errors import forbidden
from common.service_client import ServiceClient

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", DEFAULT_USER_SERVICE_URL)
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", DEFAULT_ORDER_SERVICE_URL)

# Only this role may join the delivery fleet.
RIDER_ROLE = "rider"


class UserServiceClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient("User Service", USER_SERVICE_URL, logger=logger)

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def verify_rider(self, user_id: UUID, token: str) -> dict:
        """Confirm the account exists and may currently ride.

        Looks redundant now that the token carries a role, and is not: that claim was true
        when the token was signed. An account demoted since then still presents a valid
        token until it expires, and only this lookup notices (D18).
        """
        account = self._client.get(
            f"/api/v1/users/{user_id}",
            missing=f"Account {user_id} does not exist",
            unreachable_hint="cannot verify the rider account",
            bad_gateway_hint="verifying the rider account",
            headers=bearer(token),
        )
        if account.get("role") != RIDER_ROLE:
            raise forbidden(
                f"Account {user_id} has role '{account.get('role')}' and is not authorised "
                f"to join the delivery fleet (requires '{RIDER_ROLE}')"
            )
        return account


class OrderServiceClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient("Order Service", ORDER_SERVICE_URL, logger=logger)

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def signal(self, order_id: UUID, signal: str, payload: dict) -> dict:
        """Report a delivery event into the order's workflow.

        This service does not know Temporal exists. It reports what it observed to the
        service that owns the order lifecycle, exactly as it would report any other status
        transition, and the Order Service is what relays it into the saga.
        """
        return self._client.post(
            f"/api/v1/orders/{order_id}/signals",
            json={"signal": signal, "payload": payload},
            missing=f"Order {order_id} has no running workflow to notify",
            unreachable_hint="cannot report the delivery update",
            bad_gateway_hint="reporting the delivery update",
            headers=internal_headers(),
        )
