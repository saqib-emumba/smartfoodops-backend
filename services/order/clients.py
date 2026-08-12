"""Outbound calls to sibling services.

Menus live in MongoDB behind the Menu Service, so both the pricing lookup and the audit
trail go over HTTP — this service never opens a MongoDB connection of its own.
"""

import json
import os
from logging import Logger
from uuid import UUID

from common.config import DEFAULT_MENU_SERVICE_URL
from common.service_client import ServiceClient

MENU_SERVICE_URL = os.getenv("MENU_SERVICE_URL", DEFAULT_MENU_SERVICE_URL)


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
