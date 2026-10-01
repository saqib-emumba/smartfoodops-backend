"""Internal-only: publish one event to Kafka (D53).

Replaces `payment_outbox` and its relay — see order/apis/internal_events.py for the
identical reasoning on the order side; this is the same endpoint, on this service's own
producer.
"""

from fastapi import APIRouter, Depends

from common.auth import require_internal
from common.events.topics import ORDER_EVENTS_TOPIC
from common.responses import Envelope, ok
from payment import deps
from payment.schemas.events import PublishEventRequest

router = APIRouter(prefix="/api/v1/payments", dependencies=[Depends(require_internal)])


@router.post("/internal/events", response_model=Envelope[None])
async def publish_payment_event(payload: PublishEventRequest) -> Envelope[None]:
    """`aggregate_id` is `data["order_id"]`, not a payment id (D40): payment events ride the
    same Kafka partition as the order they settle, so every consumer sees one order's events
    in order regardless of which service produced which one."""
    await deps.kafka.publish(
        ORDER_EVENTS_TOPIC,
        aggregate_type="payment",
        aggregate_id=str(payload.data["order_id"]),
        event_type=payload.event_type,
        payload=payload.data,
    )
    return ok(None, message="Published")
