"""The ingestion worker: keeps the vector store in step with the menus (Week 4, D59/D61).

Two layers, deliberately separate:

  `IngestionService`   synchronous and Kafka-free: "make this restaurant's vectors match its
                       menu right now". Everything that can go wrong with the data lives here, so
                       it is exercised the same way whether a Kafka event, a backfill or a person
                       at a shell triggers it.
  `IngestionConsumer`  the Kafka side: read `menu.published`, validate, retry, dead-letter. It
                       holds no knowledge of embeddings or SQL.

Runs as its own container from the AI Service's image (`python -m ai.ingestion`) rather than as a
lifespan task inside the API, so an embedding job — CPU-bound, seconds long for a large menu —
can never delay a search request. `--backfill` runs the service once over every published menu
and exits; the long-running worker does the same at startup, which is what makes a lost event or
a menu published before this existed a delay rather than a hole.
"""

import argparse
import asyncio
import json
import time
from datetime import datetime
from logging import Logger

import fastjsonschema
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from fastapi import HTTPException
from opentelemetry import trace
from prometheus_client import start_http_server

from common.kafka import SchemaRegistryValidator, build_producer, extract_trace_context
from ai import config
from ai.chunking import item_chunk, restaurant_chunk
from ai.clients.menu import MenuServiceClient
from ai.clients.restaurant import RestaurantServiceClient
from ai.embeddings import Embedder
from ai.metrics import (
    DOCUMENTS_INDEXED,
    EMBEDDING_LATENCY_SECONDS,
    INGESTION_EVENTS_TOTAL,
    INGESTION_LAST_EVENT_SECONDS,
)
from ai.repositories.documents import DocumentRepository

_DLQ_SUFFIX = ".dlq"
_MAX_ATTEMPTS = 5


class IngestionService:
    def __init__(
        self,
        documents: DocumentRepository,
        menus: MenuServiceClient,
        restaurants: RestaurantServiceClient,
        embedder: Embedder,
        *,
        logger: Logger,
    ):
        self._docs = documents
        self._menus = menus
        self._restaurants = restaurants
        self._embedder = embedder
        self._logger = logger

    def ingest(self, restaurant_id: str, *, event: tuple[str, str] | None = None) -> str:
        """Bring one restaurant's documents in line with its current menu.

        Returns `duplicate`, `removed`, `skipped` or `ingested`. `event` is `(event_id,
        event_type)` when a Kafka message triggered this: it is recorded as processed in the same
        transaction as the write, so a redelivered message is recognised and does nothing.

        Raises on anything transient (a sibling down, the embedder failing) so the caller can
        retry; a menu that genuinely no longer exists is not an error but a removal.
        """
        group = config.INGESTION_CONSUMER_GROUP
        if event is not None and self._docs.event_processed(group, event[0]):
            return "duplicate"
        tagged = (group, *event) if event is not None else None

        try:
            menu = self._menus.get_menu(restaurant_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            self._docs.remove_restaurant(restaurant_id)
            if tagged:
                self._docs.mark_processed(*tagged)
            return "removed"

        version = datetime.fromisoformat(menu["updated_at"])
        current = self._docs.current_version(restaurant_id)
        if (
            current is not None
            and current["source_version"] == version
            and current["embedding_model"] == self._embedder.name
        ):
            if tagged:
                self._docs.mark_processed(*tagged)
            return "skipped"

        restaurant = self._restaurants.get_restaurant(restaurant_id)
        item_rows, texts = [], []
        for category in menu["categories"]:
            for item in category["items"]:
                text = item_chunk(restaurant["name"], category["category_name"], item)
                texts.append(text)
                item_rows.append(
                    {
                        "item_key": item["item_id"],
                        "category_key": category["category_id"],
                        "category_name": category["category_name"],
                        "item_name": item["name"],
                        "restaurant_name": restaurant["name"],
                        "base_price": item["base_price"],
                        "is_available": item["is_available"],
                        "dietary_tags": item.get("dietary_tags", []),
                        "chunk_text": text,
                        "embedding_model": self._embedder.name,
                        "source_version": menu["updated_at"],
                    }
                )
        restaurant_text = restaurant_chunk(restaurant, menu["categories"])

        # One batch for the whole restaurant: the dishes and the restaurant document together.
        started = time.perf_counter()
        vectors = self._embedder.embed(texts + [restaurant_text])
        EMBEDDING_LATENCY_SECONDS.observe(time.perf_counter() - started)
        for row, vector in zip(item_rows, vectors):
            row["embedding"] = vector

        self._docs.replace_restaurant(
            {
                "restaurant_id": restaurant_id,
                "name": restaurant["name"],
                "address": restaurant.get("address"),
                "latitude": restaurant.get("latitude"),
                "longitude": restaurant.get("longitude"),
                "is_active": restaurant.get("is_active", True),
                "chunk_text": restaurant_text,
                "embedding": vectors[-1],
                "embedding_model": self._embedder.name,
                "source_version": menu["updated_at"],
            },
            item_rows,
            event=tagged,
        )
        self._logger.info(
            "Indexed restaurant %s: %d dishes (menu version %s)",
            restaurant_id, len(item_rows), menu["updated_at"],
        )
        return "ingested"

    def backfill(self) -> dict[str, int]:
        """Ingest every published menu, skipping those already current. Returns outcome counts.

        One restaurant failing is logged and counted, not raised: a menu the embedder chokes on
        must not stop the other hundred from being indexed. Listing the menus is different — if
        that fails there is nothing to iterate, so it propagates for the caller to retry.
        """
        outcomes: dict[str, int] = {}
        for ref in self._menus.list_published():
            restaurant_id = str(ref["restaurant_id"])
            try:
                outcome = self.ingest(restaurant_id)
            except Exception as exc:  # noqa: BLE001 - see docstring
                self._logger.warning("Backfill could not index %s: %s", restaurant_id, exc)
                outcome = "failed"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        self.refresh_gauges()
        return outcomes

    def refresh_gauges(self) -> None:
        for kind, count in self._docs.counts().items():
            DOCUMENTS_INDEXED.labels(kind=kind).set(count)


class IngestionConsumer:
    def __init__(
        self,
        service: IngestionService,
        *,
        topic: str,
        bootstrap_servers: str,
        schema_registry_url: str,
        logger: Logger,
        poll_interval: float = 1.0,
    ):
        self._service = service
        self._topic = topic
        self._dlq_topic = topic + _DLQ_SUFFIX
        self._bootstrap_servers = bootstrap_servers
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
                group_id=config.INGESTION_CONSUMER_GROUP,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                isolation_level="read_committed",
            )
            await consumer.start()
            dlq_producer = build_producer(self._bootstrap_servers)
            await dlq_producer.start()
        except KafkaError as exc:
            self._logger.warning("Ingestion consumer: Kafka unreachable, will retry: %s", exc)
            return False
        self._consumer = consumer
        self._dlq_producer = dlq_producer
        self._logger.info(
            "Ingestion consumer connected, group=%s topic=%s",
            config.INGESTION_CONSUMER_GROUP, self._topic,
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
                    # The database write (inside `_handle`) lands before this offset commit:
                    # at-least-once, made effectively-once by the processed_events dedup.
                    await self._consumer.commit()
            except asyncio.CancelledError:
                raise
            except KafkaError as exc:
                self._logger.error("Ingestion consumer: Kafka error, reconnecting: %s", exc)
                if self._consumer is not None:
                    await self._consumer.stop()
                    self._consumer = None
                await asyncio.sleep(self._poll_interval)

    async def _handle(self, msg) -> None:
        INGESTION_LAST_EVENT_SECONDS.set_to_current_time()
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
            "ai.ingest", context=extract_trace_context(msg.headers), kind=trace.SpanKind.CONSUMER
        ):
            try:
                envelope = json.loads(msg.value.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                await self._to_dlq(msg, reason=f"undecodable payload: {exc}")
                return

            event_type = envelope.get("event_type")
            if event_type != "menu.published":
                # An unrecognised type is a producer shipping something this consumer has no use
                # for yet, not a poison message (D40's consumer rule): skip, never dead-letter.
                self._logger.info("Ingestion consumer: skipping event_type=%s", event_type)
                return

            data = envelope.get("data", {})
            try:
                self._schema_validator.validate(event_type, envelope.get("event_version", 1), data)
            except fastjsonschema.JsonSchemaException as exc:
                await self._to_dlq(msg, reason=f"schema validation failed: {exc}")
                return

            await self._ingest_with_retry(msg, str(data["restaurant_id"]), envelope["event_id"], event_type)

    async def _ingest_with_retry(self, msg, restaurant_id: str, event_id: str, event_type: str) -> None:
        """Retry transient failures with backoff, then dead-letter.

        Committing past a message that has not been handled would lose it, so a failure may not
        simply propagate: the loop above would carry on to the next record and its commit would
        step over this one. Retrying here keeps the partition at this message until it succeeds
        or is moved to the DLQ — and the startup backfill recovers a dead-lettered one anyway.
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                outcome = await asyncio.to_thread(
                    self._service.ingest, restaurant_id, event=(event_id, event_type)
                )
                INGESTION_EVENTS_TOTAL.labels(outcome=outcome).inc()
                await asyncio.to_thread(self._service.refresh_gauges)
                return
            except Exception as exc:  # noqa: BLE001 - any failure here is retried, then dead-lettered
                self._logger.warning(
                    "Ingestion of %s failed (attempt %d/%d): %s", restaurant_id, attempt, _MAX_ATTEMPTS, exc
                )
                if attempt == _MAX_ATTEMPTS:
                    INGESTION_EVENTS_TOTAL.labels(outcome="dlq").inc()
                    await self._to_dlq(msg, reason=f"ingestion failed after {_MAX_ATTEMPTS} attempts: {exc}")
                    return
                await asyncio.sleep(2 ** attempt)

    async def _to_dlq(self, msg, *, reason: str) -> None:
        if self._dlq_producer is None:
            self._logger.error("Ingestion consumer: no DLQ producer available for: %s", reason)
            return
        headers = list(msg.headers or [])
        headers.append(("x-dlq-reason", reason.encode("utf-8")))
        headers.append(("x-dlq-original-topic", self._topic.encode("utf-8")))
        await self._dlq_producer.send_and_wait(
            self._dlq_topic, key=msg.key, value=msg.value, headers=headers
        )
        self._logger.warning("Ingestion consumer: sent to DLQ (%s): %s", self._dlq_topic, reason)


async def _startup_backfill(service: IngestionService, logger: Logger) -> None:
    """Reconcile at startup, retrying while the menu service comes up."""
    for attempt in range(1, 6):
        try:
            outcomes = await asyncio.to_thread(service.backfill)
            logger.info("Startup backfill complete: %s", outcomes or "no published menus")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Startup backfill attempt %d/5 failed: %s", attempt, exc)
            await asyncio.sleep(5 * attempt)
    logger.error("Startup backfill gave up; events will still be ingested as they arrive")


async def main(backfill_only: bool) -> None:
    import signal

    from common.config import service_url
    from common.events.topics import MENU_EVENTS_TOPIC
    from ai import deps

    logger = deps.logger
    service = IngestionService(
        DocumentRepository(deps.db, logger=logger),
        MenuServiceClient(logger),
        RestaurantServiceClient(logger),
        deps.embedder,
        logger=logger,
    )

    async with deps.db.lifespan(None):
        if backfill_only:
            outcomes = await asyncio.to_thread(service.backfill)
            logger.info("Backfill complete: %s", outcomes or "no published menus")
            print(json.dumps(outcomes))
            return

        start_http_server(config.INGESTION_METRICS_PORT)
        logger.info("Prometheus metrics server listening on :%d", config.INGESTION_METRICS_PORT)
        await asyncio.to_thread(service.refresh_gauges)

        consumer = IngestionConsumer(
            service,
            topic=MENU_EVENTS_TOPIC,
            bootstrap_servers=service_url("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
            schema_registry_url=service_url("SCHEMA_REGISTRY_URL", "http://schema-registry:8081"),
            logger=logger,
        )
        tasks = [
            asyncio.create_task(_startup_backfill(service, logger)),
            asyncio.create_task(consumer.run_forever()),
        ]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Service ingestion worker")
    parser.add_argument(
        "--backfill", action="store_true",
        help="ingest every published menu once, then exit (instead of consuming events)",
    )
    asyncio.run(main(parser.parse_args().backfill))
