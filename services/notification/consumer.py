"""The Kafka-to-Celery bridge: `python -m notification.consumer` is this file's entrypoint
(Week 3, D45).

Reads `sfo.order.events.v1` under its own consumer group ("notification"), independent of
analytics-service's — Kafka consumer groups give each subscriber its own full copy of the
stream, so the two services never compete for messages or need to coordinate.

Commits its Kafka offset only *after* `celery_app.send_task` returns, which is what makes
Kafka the durable ledger rather than RabbitMQ: a crash between enqueueing the task and
committing the offset redelivers the same event, and enqueues the task again — an accepted,
named trade-off (see `notification/__init__.py`), not the platform's usual at-least-once-
with-dedup discipline, because a duplicate SMS costs nothing like a duplicate charge does.

Structured exactly like `analytics/consumer.py`'s reconnect loop and schema-validation
gate, not a fire-and-forget daemon thread — the blueprint this replaces started its bridge
thread at *module import*, inside the Celery client process, which silently died on the
first connection error and ran in the wrong process to boot (threads are not inherited
across `fork()`, so Celery's prefork workers never even got it).
"""

import asyncio
import json
from logging import Logger

import fastjsonschema
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from opentelemetry import trace
from prometheus_client import Counter, Gauge, start_http_server

from common.kafka import SchemaRegistryValidator, build_producer, extract_trace_context
from notification.clients.user import UserServiceClient
from notification.worker import celery_app

_GROUP_ID = "notification"
_DLQ_SUFFIX = ".dlq"

# This process has no FastAPI app and no /metrics route of its own — same reasoning as
# orchestrator-worker's own bare prometheus_client server (services/orchestrator/worker.py).
METRICS_PORT = 9110

NOTIFICATIONS_SENT_TOTAL = Counter(
    "sfo_notifications_enqueued_total", "Notification tasks enqueued", ["channel", "status"]
)
CONSUMER_LAST_MESSAGE_SECONDS = Gauge(
    "sfo_consumer_last_message_timestamp_seconds",
    "Unix time of the last message this consumer processed",
    ["consumer"],
)
_LAST_MESSAGE = CONSUMER_LAST_MESSAGE_SECONDS.labels(consumer=_GROUP_ID)

# order.confirmed: payment settled and the kitchen slot claimed — the first moment worth
# telling a customer anything. order.delivered / order.cancelled: the two terminal facts.
# Not order.assigned/picked_up/kitchen.decided/payment.*: notification-worthy is a much
# smaller set than event-worthy, which is the whole reason this is a filtered subscriber
# rather than another copy of analytics' full projection.
_NOTIFIABLE_EVENTS = {"order.confirmed", "order.delivered", "order.cancelled"}


class NotificationConsumer:
    def __init__(
        self,
        *,
        topic: str,
        bootstrap_servers: str,
        schema_registry_url: str,
        user_service: UserServiceClient,
        logger: Logger,
        poll_interval: float = 1.0,
    ):
        self._topic = topic
        self._dlq_topic = topic + _DLQ_SUFFIX
        self._bootstrap_servers = bootstrap_servers
        self._user_service = user_service
        self._logger = logger
        self._poll_interval = poll_interval
        self._schema_validator = SchemaRegistryValidator(schema_registry_url)
        self._consumer: AIOKafkaConsumer | None = None
        self._dlq_producer = None

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
            self._logger.warning("Notification consumer: Kafka unreachable, will retry: %s", exc)
            return False
        self._consumer = consumer
        self._dlq_producer = dlq_producer
        self._logger.info("Notification consumer connected, group=%s topic=%s", _GROUP_ID, self._topic)
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                if not await self._ensure_consumer():
                    await asyncio.sleep(self._poll_interval)
                    continue
                async for msg in self._consumer:
                    await self._handle(msg)
                    await self._consumer.commit()
            except asyncio.CancelledError:
                raise
            except KafkaError as exc:
                self._logger.error("Notification consumer: Kafka error, reconnecting: %s", exc)
                if self._consumer is not None:
                    await self._consumer.stop()
                    self._consumer = None
                await asyncio.sleep(self._poll_interval)
            except Exception as exc:  # noqa: BLE001 - must never crash the process
                self._logger.error("Notification consumer: unexpected error: %s", exc)
                await asyncio.sleep(self._poll_interval)

    async def _handle(self, msg) -> None:
        _LAST_MESSAGE.set_to_current_time()
        context = extract_trace_context(msg.headers)
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
            "notification.consume", context=context, kind=trace.SpanKind.CONSUMER
        ):
            try:
                envelope = json.loads(msg.value.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                await self._to_dlq(msg, reason=f"undecodable payload: {exc}")
                return

            event_type = envelope.get("event_type")
            if event_type not in _NOTIFIABLE_EVENTS:
                # Includes every KNOWN event type this consumer simply doesn't act on
                # (order.created, order.assigned, payment.*, ...), not only unrecognised
                # ones — a much larger "not for me" set than analytics has, and none of it
                # goes through schema validation, since this consumer never touches the
                # data of an event it isn't going to use.
                return

            event_version = envelope.get("event_version", 1)
            data = envelope.get("data", {})
            try:
                self._schema_validator.validate(event_type, event_version, data)
            except fastjsonschema.JsonSchemaException as exc:
                await self._to_dlq(msg, reason=f"schema validation failed: {exc}")
                return

            await self._notify(event_type, data)

    async def _notify(self, event_type: str, data: dict) -> None:
        customer_id = data.get("customer_id")
        order_id = data.get("order_id")
        if not customer_id or not order_id:
            return
        try:
            contact = await self._user_service.fetch_contact_details(customer_id)
        except Exception as exc:  # noqa: BLE001 - a lookup failure must not wedge the consumer
            self._logger.warning(
                "Notification consumer: could not resolve contact for customer %s: %s",
                customer_id, exc,
            )
            return

        phone, email = contact.get("phone"), contact.get("email")
        status = {"order.confirmed": "confirmed", "order.delivered": "delivered",
                  "order.cancelled": "cancelled"}[event_type]
        detail = data.get("metadata", {}).get("reason") if event_type == "order.cancelled" else None

        # celery_app.send_task is a synchronous client call (it publishes to RabbitMQ and
        # returns); asyncio.to_thread keeps it off the event loop, same discipline the
        # outbox relay applies to its own blocking psycopg2 calls.
        if phone:
            await asyncio.to_thread(
                celery_app.send_task, "notification.dispatch_sms",
                args=[order_id, status, phone, detail],
            )
            NOTIFICATIONS_SENT_TOTAL.labels(channel="sms", status=status).inc()
        if email and event_type == "order.confirmed":
            # Email only on confirmation — matching the mock gateway's own two-touch
            # pattern (SMS at every step, an invoice-shaped email once, at the start).
            await asyncio.to_thread(
                celery_app.send_task, "notification.dispatch_email",
                args=[order_id, status, email, detail],
            )
            NOTIFICATIONS_SENT_TOTAL.labels(channel="email", status=status).inc()

    async def _to_dlq(self, msg, *, reason: str) -> None:
        if self._dlq_producer is None:
            self._logger.error("Notification consumer: no DLQ producer available for: %s", reason)
            return
        headers = list(msg.headers or [])
        headers.append(("x-dlq-reason", reason.encode("utf-8")))
        headers.append(("x-dlq-original-topic", self._topic.encode("utf-8")))
        await self._dlq_producer.send_and_wait(
            self._dlq_topic, key=msg.key, value=msg.value, headers=headers
        )
        self._logger.warning("Notification consumer: sent to DLQ (%s): %s", self._dlq_topic, reason)


async def main() -> None:
    import signal

    from common.bootstrap import bootstrap
    from common.config import service_url
    from common.events.topics import ORDER_EVENTS_TOPIC

    runtime = bootstrap("notification-consumer", db=False)
    logger = runtime.logger

    start_http_server(METRICS_PORT)
    logger.info("Prometheus metrics server listening on :%d", METRICS_PORT)

    consumer = NotificationConsumer(
        topic=ORDER_EVENTS_TOPIC,
        bootstrap_servers=service_url("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
        schema_registry_url=service_url("SCHEMA_REGISTRY_URL", "http://schema-registry:8081"),
        user_service=UserServiceClient(logger),
        logger=logger,
    )

    task = asyncio.create_task(consumer.run_forever())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
