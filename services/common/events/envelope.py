"""The one envelope every event on the platform's Kafka topic wears (Week 3, D40).

Mirrors D35's reasoning for HTTP responses — one shape, everywhere, so a consumer parses
without first guessing which event it received — applied to the async side of the platform
instead of the synchronous one.

`data` is deliberately untyped here: what belongs in it is decided per `event_type` by the
models in `order.py` and `payment.py`, and validated against those at the boundary (the
outbox insert on the way out, the consumer on the way in) rather than by this class, which
only owns the envelope shape common to all of them.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class EventEnvelope(BaseModel):
    """The wire shape. Field order matches the outbox table's columns (D39) on purpose —
    a row read out of `order_outbox`/`payment_outbox` maps onto this with no translation
    layer between the database and the wire.
    """

    event_id: UUID
    event_type: str
    event_version: int = 1
    aggregate_type: str
    aggregate_id: UUID
    seq: int
    occurred_at: datetime
    producer: str
    data: dict[str, Any]
