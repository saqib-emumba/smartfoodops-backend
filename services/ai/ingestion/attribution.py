"""Attributing orders to assistant recommendations (Week 5, D63).

A consumer of `order.created` that asks one question per event: did the assistant name this
restaurant to this customer recently? If so the order is counted against that interaction and its
value added to `sfo_ai_attributed_revenue_total`. That is what lets the platform report the two
business numbers the spec asks for — acceptance rate and order conversion after an AI interaction —
without the Order Service knowing the assistant exists or the checkout request carrying anything new.

Deliberately lighter than the ingestion consumer: no schema registry lookup and no dead-letter
topic. The event comes from the platform's own producer and is parsed with the same pydantic model,
and a malformed one is counted and skipped; an attribution that is dropped costs a data point on a
dashboard, whereas a menu update that is dropped leaves search wrong, which is what the stronger
machinery in `ai.ingestion` is for. The commit follows the write, and the attribution is idempotent
(see `repositories/interactions.py`), so redelivery does no harm.
"""

import asyncio
import json
from logging import Logger

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from pydantic import ValidationError

from common.events.order import OrderCreatedData
from ai import config
from ai.metrics import ATTRIBUTED_ORDERS_TOTAL, ATTRIBUTED_REVENUE_TOTAL, ATTRIBUTION_EVENTS_TOTAL
from ai.repositories.interactions import InteractionRepository


class AttributionConsumer:
    def __init__(
        self,
        interactions: InteractionRepository,
        *,
        topic: str,
        bootstrap_servers: str,
        logger: Logger,
        poll_interval: float = 1.0,
    ):
        self._interactions = interactions
        self._topic = topic
        self._bootstrap_servers = bootstrap_servers
        self._logger = logger
        self._poll_interval = poll_interval
        self._consumer: AIOKafkaConsumer | None = None

    async def _ensure_consumer(self) -> bool:
        if self._consumer is not None:
            return True
        try:
            consumer = AIOKafkaConsumer(
                self._topic,
                bootstrap_servers=self._bootstrap_servers,
                group_id=config.ATTRIBUTION_CONSUMER_GROUP,
                enable_auto_commit=False,
                # A new deployment attributes from now on rather than replaying history: an order
                # older than the window could never match a recommendation anyway.
                auto_offset_reset="latest",
                isolation_level="read_committed",
            )
            await consumer.start()
        except KafkaError as exc:
            self._logger.warning("Attribution consumer: Kafka unreachable, will retry: %s", exc)
            return False
        self._consumer = consumer
        self._logger.info(
            "Attribution consumer connected, group=%s topic=%s", config.ATTRIBUTION_CONSUMER_GROUP, self._topic
        )
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
                self._logger.error("Attribution consumer: Kafka error, reconnecting: %s", exc)
                if self._consumer is not None:
                    await self._consumer.stop()
                    self._consumer = None
                await asyncio.sleep(self._poll_interval)

    async def _handle(self, msg) -> None:
        try:
            envelope = json.loads(msg.value.decode("utf-8"))
            if envelope.get("event_type") != "order.created":
                return  # every other order event is none of this consumer's business
            order = OrderCreatedData(**envelope["data"])
        except (ValueError, KeyError, ValidationError) as exc:
            ATTRIBUTION_EVENTS_TOTAL.labels(outcome="malformed").inc()
            self._logger.warning("Attribution consumer: skipping malformed event: %s", exc)
            return

        try:
            attributed = await asyncio.to_thread(
                self._interactions.attribute,
                customer_id=str(order.customer_id),
                restaurant_id=str(order.restaurant_id),
                order_id=str(order.order_id),
            )
        except Exception as exc:  # noqa: BLE001 - a lost data point must not stall the partition
            self._logger.error("Attribution failed for order %s: %s", order.order_id, exc)
            return
        if attributed:
            ATTRIBUTION_EVENTS_TOTAL.labels(outcome="attributed").inc()
            ATTRIBUTED_ORDERS_TOTAL.inc()
            ATTRIBUTED_REVENUE_TOTAL.inc(float(order.total_amount))
        else:
            ATTRIBUTION_EVENTS_TOTAL.labels(outcome="no_match").inc()
