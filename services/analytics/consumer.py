"""The Kafka consumer: reads `sfo.order.events.v1`, derives `order_projections`, and keeps
the Prometheus metrics `/metrics` serves in sync (Week 3, D40).

Runs as a lifespan-managed background task with its own reconnect loop — not the
blueprint's fire-and-forget daemon thread, which died silently on the first connection
error and left the service reporting healthy with a permanently dead consumer. A
`KafkaConnectionError` here is caught, logged, and retried on the same cadence as the
outbox relay's own reconnect loop in `common/outbox.py`.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from logging import Logger

import fastjsonschema
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from fastapi import FastAPI
from opentelemetry import trace
from prometheus_client import Counter, Gauge

from common.kafka import SchemaRegistryValidator, build_producer, extract_trace_context
from analytics.repositories.projections import ProjectionsRepository

_GROUP_ID = "analytics"
_DLQ_SUFFIX = ".dlq"

# Module-level, declared once — never inside the message loop. The blueprint this replaces
# constructed a Gauge per message, which raised `Duplicated timeseries in CollectorRegistry`
# on the very first one; module scope is what makes that class of bug structurally
# impossible here.
ORDERS_PLACED_TOTAL = Counter("sfo_business_orders_placed_total", "Orders placed")
ORDERS_DELIVERED_TOTAL = Counter("sfo_business_orders_delivered_total", "Orders delivered")
ORDERS_CANCELLED_TOTAL = Counter("sfo_business_orders_cancelled_total", "Orders cancelled")
AVERAGE_DELIVERY_SECONDS = Gauge(
    "sfo_business_average_delivery_seconds", "Average time from placed to delivered"
)
CONSUMER_LAST_MESSAGE_SECONDS = Gauge(
    "sfo_consumer_last_message_timestamp_seconds",
    "Unix time of the last message this consumer processed — a flat line means a wedged consumer",
)

# Terminal/status event types this projection tracks. `payment.*` and
# `order.kitchen.decided` are schema-validated and offset-committed like any other event,
# but carry no order-lifecycle status of their own, so they update no projection row.
_STATUS_EVENT_TYPES = {"order.confirmed", "order.assigned", "order.picked_up"}

logger_name = "analytics.consumer"


class AnalyticsConsumer:
    def __init__(
        self,
        projections: ProjectionsRepository,
        *,
        topic: str,
        bootstrap_servers: str,
        schema_registry_url: str,
        logger: Logger,
        poll_interval: float = 1.0,
    ):
        self._projections = projections
        self._topic = topic
        self._dlq_topic = topic + _DLQ_SUFFIX
        self._bootstrap_servers = bootstrap_servers
        self._logger = logger
        self._poll_interval = poll_interval
        self._schema_validator = SchemaRegistryValidator(schema_registry_url)
        self._consumer: AIOKafkaConsumer | None = None
        self._dlq_producer = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @asynccontextmanager
    async def lifespan(self, _: FastAPI = None):
        # Seed the Counters/Gauge from durable state before serving any traffic, so a
        # restart does not silently reset business totals to zero — see
        # repositories/projections.py's comment on why this needs a Counter's private
        # `_value.set(...)`, which prometheus_client exposes for exactly this purpose.
        self._seed_metrics()
        self._task = asyncio.create_task(self._run(), name="analytics-consumer")
        try:
            yield
        finally:
            self._stopping.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            if self._consumer is not None:
                await self._consumer.stop()
            if self._dlq_producer is not None:
                await self._dlq_producer.stop()

    def _seed_metrics(self) -> None:
        counts = self._projections.status_counts()
        # "Placed" = every row that ever existed, not just those still `created` — a
        # delivered or cancelled order was placed too. Sum every status this table knows.
        placed = sum(counts.values())
        ORDERS_PLACED_TOTAL._value.set(placed)
        ORDERS_DELIVERED_TOTAL._value.set(counts.get("delivered", 0))
        ORDERS_CANCELLED_TOTAL._value.set(counts.get("cancelled", 0))
        avg = self._projections.average_delivery_seconds()
        if avg is not None:
            AVERAGE_DELIVERY_SECONDS.set(avg)
        self._logger.info(
            "Analytics consumer seeded from %d existing projection rows", placed
        )

    async def _ensure_consumer(self) -> bool:
        if self._consumer is not None:
            return True
        try:
            consumer = AIOKafkaConsumer(
                self._topic,
                bootstrap_servers=self._bootstrap_servers,
                group_id=_GROUP_ID,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                isolation_level="read_committed",
            )
            await consumer.start()
            dlq_producer = build_producer(self._bootstrap_servers)
            await dlq_producer.start()
        except KafkaError as exc:
            self._logger.warning("Analytics consumer: Kafka unreachable, will retry: %s", exc)
            return False
        self._consumer = consumer
        self._dlq_producer = dlq_producer
        self._logger.info("Analytics consumer connected, group=%s topic=%s", _GROUP_ID, self._topic)
        return True

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                if not await self._ensure_consumer():
                    await asyncio.sleep(self._poll_interval)
                    continue
                async for msg in self._consumer:
                    await self._handle(msg)
                    # DB commit (inside _handle, via ProjectionsRepository) happens before
                    # this offset commit — at-least-once, effectively-once given the
                    # processed_events dedup. Never the other way around.
                    await self._consumer.commit()
                    if self._stopping.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except KafkaError as exc:
                self._logger.error("Analytics consumer: Kafka error, reconnecting: %s", exc)
                if self._consumer is not None:
                    await self._consumer.stop()
                    self._consumer = None
                await asyncio.sleep(self._poll_interval)
            except Exception as exc:  # noqa: BLE001 - the consumer must never crash the process
                self._logger.error("Analytics consumer: unexpected error: %s", exc)
                await asyncio.sleep(self._poll_interval)

    async def _handle(self, msg) -> None:
        CONSUMER_LAST_MESSAGE_SECONDS.set_to_current_time()
        context = extract_trace_context(msg.headers)
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
            "analytics.consume", context=context, kind=trace.SpanKind.CONSUMER
        ):
            try:
                envelope = json.loads(msg.value.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                # Not schema-invalid, not unknown — genuinely unparseable. Non-transient:
                # retrying the same bytes will never succeed, so this is the one case that
                # goes to the DLQ without ever reaching the event_type/version dispatch.
                await self._to_dlq(msg, reason=f"undecodable payload: {exc}")
                return

            event_type = envelope.get("event_type")
            event_version = envelope.get("event_version", 1)
            data = envelope.get("data", {})
            event_id = envelope.get("event_id")
            occurred_at = envelope.get("occurred_at")

            if event_type not in _KNOWN_EVENT_TYPES():
                # Consumer rule (D40): an unrecognised event_type is a producer shipping a
                # feature this consumer doesn't know about yet, not a poison message — log
                # and move on, never DLQ. DLQ is reserved for a KNOWN type whose payload
                # fails its own schema.
                self._logger.info("Analytics consumer: skipping unknown event_type=%s", event_type)
                return

            try:
                self._schema_validator.validate(event_type, event_version, data)
            except fastjsonschema.JsonSchemaException as exc:
                await self._to_dlq(msg, reason=f"schema validation failed: {exc}")
                return

            applied = self._apply(event_id, event_type, data, occurred_at)
            if applied:
                self._advance_metrics(event_type)

    def _apply(self, event_id: str, event_type: str, data: dict, occurred_at: str) -> bool:
        if event_type == "order.created":
            return self._projections.apply_order_created(
                event_id=event_id, data=data, occurred_at=occurred_at
            )
        if event_type in _STATUS_EVENT_TYPES:
            return self._projections.apply_order_status(
                event_id=event_id, event_type=event_type, status=data["new_status"], data=data
            )
        if event_type == "order.delivered":
            return self._projections.apply_order_delivered(
                event_id=event_id, data=data, occurred_at=occurred_at
            )
        if event_type == "order.cancelled":
            return self._projections.apply_order_cancelled(
                event_id=event_id, data=data, occurred_at=occurred_at
            )
        # payment.* and order.kitchen.decided: acknowledged (dedup-recorded so a replay
        # is a no-op) but carry no projection update of their own.
        return self._projections.mark_seen(event_id=event_id, event_type=event_type)

    def _advance_metrics(self, event_type: str) -> None:
        if event_type == "order.created":
            ORDERS_PLACED_TOTAL.inc()
        elif event_type == "order.delivered":
            ORDERS_DELIVERED_TOTAL.inc()
            avg = self._projections.average_delivery_seconds()
            if avg is not None:
                AVERAGE_DELIVERY_SECONDS.set(avg)
        elif event_type == "order.cancelled":
            ORDERS_CANCELLED_TOTAL.inc()

    async def _to_dlq(self, msg, *, reason: str) -> None:
        if self._dlq_producer is None:
            self._logger.error("Analytics consumer: no DLQ producer available for: %s", reason)
            return
        headers = list(msg.headers or [])
        headers.append(("x-dlq-reason", reason.encode("utf-8")))
        headers.append(("x-dlq-original-topic", self._topic.encode("utf-8")))
        await self._dlq_producer.send_and_wait(
            self._dlq_topic, key=msg.key, value=msg.value, headers=headers
        )
        self._logger.warning("Analytics consumer: sent to DLQ (%s): %s", self._dlq_topic, reason)


def _KNOWN_EVENT_TYPES() -> set[str]:
    from common.events.order import EVENT_DATA_MODELS as ORDER_MODELS
    from common.events.payment import EVENT_DATA_MODELS as PAYMENT_MODELS

    return set(ORDER_MODELS) | set(PAYMENT_MODELS)
