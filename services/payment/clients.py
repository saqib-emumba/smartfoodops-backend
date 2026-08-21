"""Outbound calls to sibling services.

A payment references exactly one thing this service's database does not hold: the order it
settles. That reference was a real foreign key while `payments` lived in the Order Service's
database; under database-per-service no foreign key can span the two, so the order is
resolved over HTTP immediately before the write instead.

An unknown order surfaces as 422 rather than 404, matching the Order Service's own choice
for an unknown customer: the request is well formed and the payment is not the thing that is
missing — the entity it points at is.
"""

import os
from logging import Logger
from uuid import UUID

from common.auth import bearer, internal_headers
from common.config import DEFAULT_ORDER_SERVICE_URL
from common.errors import unprocessable
from common.service_client import ServiceClient

ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", DEFAULT_ORDER_SERVICE_URL)


class OrderServiceClient:
    def __init__(self, logger: Logger):
        self._client = ServiceClient("Order Service", ORDER_SERVICE_URL, logger=logger)

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def fetch_order(self, order_id: UUID, token: str) -> dict:
        """Confirm the order exists and return it — the check `order_id` lost with its FK.

        The response carries the server-recalculated `total_amount`, which is what the
        requested payment amount is then checked against (see amounts.py).

        Doubles as the ownership check for a payment. The Order Service only serves an
        order to the customer who placed it, so forwarding the caller's token means paying
        for someone else's order is refused there and reaches us as a 403.
        """
        return self._client.get(
            f"/api/v1/orders/{order_id}",
            missing=f"Unknown order {order_id} referenced by this payment",
            missing_error=unprocessable,
            unreachable_hint="cannot verify the order",
            bad_gateway_hint="verifying the order",
            headers=bearer(token),
        )

    def fetch_order_internally(self, order_id: UUID) -> dict:
        """Read an order on the internal key, for calls that have no user behind them.

        The saga's payment activity needs the same authoritative `total_amount` that
        `fetch_order` returns, but it holds no bearer token to forward: a workflow is not a
        user, and a token placed in a workflow argument would be written into durable,
        UI-visible history and would expire mid-saga anyway (D26).

        The ownership check `fetch_order` gets for free is not lost, only moved. The saga
        did not choose this order — it was started by an already-authorised
        POST /api/v1/orders whose handler verified the caller owns it.
        """
        return self._client.get(
            f"/api/v1/orders/{order_id}/internal",
            missing=f"Unknown order {order_id} referenced by this payment",
            missing_error=unprocessable,
            unreachable_hint="cannot verify the order",
            bad_gateway_hint="verifying the order",
            headers=internal_headers(),
        )
