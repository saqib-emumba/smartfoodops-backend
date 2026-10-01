"""Internal-only: publish one event to Kafka (D53).

Replaces `order_outbox` and its relay. Deliberately dumb — validate and produce, nothing
else — because the caller (`publish_order_event_activity`, called from `OrderWorkflow` right
after the activity that made the underlying write) has already read the order fresh and
assembled `data` itself; this endpoint's only job is the one thing a Temporal activity cannot
do safely inside a `@workflow.defn` body (D38): talk to Kafka.

Internal-key only, same convention every other internal route in this service uses.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from common.auth import require_internal
from common.events.topics import ORDER_EVENTS_TOPIC
from common.responses import Envelope, ok
from order import deps
from order.schemas.events import PublishEventRequest

router = APIRouter(prefix="/api/v1/orders", dependencies=[Depends(require_internal)])


@router.post("/{order_id}/internal/events", response_model=Envelope[None])
async def publish_order_event(
    order_id: UUID, payload: PublishEventRequest
) -> Envelope[None]:
    await deps.kafka.publish(
        ORDER_EVENTS_TOPIC,
        aggregate_type="order",
        aggregate_id=str(order_id),
        event_type=payload.event_type,
        payload=payload.data,
    )
    return ok(None, message="Published")
