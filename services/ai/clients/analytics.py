"""The Analytics Service, as the RAG assembler reads it (internal route, D62)."""

from common.auth import internal_headers
from common.service_client import ServiceFacade

DEFAULT_ANALYTICS_SERVICE_URL = "http://analytics-service:8008"


class AnalyticsServiceClient(ServiceFacade):
    display_name = "Analytics Service"
    env_var = "ANALYTICS_SERVICE_URL"
    default_url = DEFAULT_ANALYTICS_SERVICE_URL

    def customer_summary(self, customer_id: str) -> dict:
        """Order counts and favourite restaurants. An unknown customer is zeros, not a 404."""
        return self._client.get(
            f"/api/v1/analytics/internal/customers/{customer_id}/summary",
            missing=f"No analytics for customer {customer_id}",
            unreachable_hint="cannot read order history",
            headers=internal_headers(),
        )
