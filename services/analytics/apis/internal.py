"""Internal-only reads over the order projection (Week 4, D62).

The Analytics Service had no read API at all until now — everything it knows came from Kafka and
only Prometheus looked at it. The AI Service's RAG assembler needs "what has this customer
ordered", and database-per-service (D01) means it asks here rather than reading
`sfo_analytics_core` itself.

Internal-key only, and with no `route_permissions` row (nor an nginx location at all — the
gateway has never routed this service), so it is reachable only from inside the network.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from common.auth import require_internal
from common.responses import Envelope, ok
from analytics import deps
from analytics.schemas.customers import CustomerSummary

router = APIRouter(prefix="/api/v1/analytics", dependencies=[Depends(require_internal)])


@router.get(
    "/internal/customers/{customer_id}/summary",
    response_model=Envelope[CustomerSummary],
)
def customer_summary(
    customer_id: UUID,
    top: int = Query(3, ge=1, le=10, description="How many favourite restaurants to return"),
) -> Envelope[CustomerSummary]:
    """Order counts and favourite restaurants for one customer.

    A customer with no orders is a `200` with zeros and an empty list, not a `404`: "no
    history" is an answer a recommendation can use ("nothing to personalise from"), and treating
    it as an error would make every new customer's assistant request fail.
    """
    return ok(
        CustomerSummary(**deps.projections.customer_summary(customer_id, top=top)),
        message="Customer summary",
    )
