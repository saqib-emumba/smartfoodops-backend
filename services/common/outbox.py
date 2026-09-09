"""The transactional outbox (Week 3, D39): `append_outbox` here, `OutboxRelay` added
alongside it once Kafka exists.

Genuinely infrastructure, not domain knowledge, despite writing into a table that lives in
each service's own database: `order_outbox` and `payment_outbox` share the exact same shape
— same columns, same partial index, same claim query — so one function parameterised by
table name is the whole implementation. Duplicating it into `order/repositories/sql.py` and
a payment equivalent would be two copies of one idea, which is exactly what
`common/__init__.py`'s charter exists to prevent. What is genuinely order-specific —
`order_tracking_logs`, `append_log`, the CAS in `TRANSITION_ORDER` — stays exactly where it
is; this module knows nothing about orders, only about outbox rows.

`append_outbox` takes an already-leased cursor, the same idiom `order/repositories/sql.py`'s
`append_log` already established: the caller owns the transaction, this function only adds
one more statement to it. Called from inside the same `cursor(commit=True)` block that
writes the business row, so an event can never exist for a write that didn't happen.
"""

import json
from decimal import Decimal
from logging import Logger
from typing import Any
from uuid import UUID

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from psycopg2.extras import Json

# The only two tables this module is ever asked to write to. Table names are interpolated
# into SQL text below rather than bound as a parameter (psycopg2 cannot parameterise an
# identifier), so this allowlist is a real guard, not a formality — every caller passes a
# literal from this set, never anything derived from a request.
_ALLOWED_TABLES = {"order_outbox", "payment_outbox"}

_propagator = TraceContextTextMapPropagator()


def _json_default(value: Any) -> Any:
    """`Decimal` shows up in every payload here (D07: money is Decimal until the JSON
    boundary) and the stdlib encoder has no idea what to do with one."""
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def append_outbox(
    cur,
    *,
    table: str,
    aggregate_type: str,
    aggregate_id: UUID | str,
    event_type: str,
    payload: dict,
    event_version: int = 1,
) -> dict:
    """Insert one outbox row on an already-leased cursor. Returns the row written.

    Captures the *request's* current trace context, not the relay's — the relay runs
    asynchronously, potentially minutes later, and injecting its own context here would
    start a new, disconnected trace instead of continuing the one the write belongs to.
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"append_outbox: unknown outbox table {table!r}")

    carrier: dict[str, str] = {}
    _propagator.inject(carrier)

    cur.execute(
        f"""
        INSERT INTO {table}
            (aggregate_type, aggregate_id, event_type, event_version, payload,
             traceparent, tracestate)
        VALUES (%(aggregate_type)s, %(aggregate_id)s, %(event_type)s, %(event_version)s,
                %(payload)s, %(traceparent)s, %(tracestate)s)
        RETURNING id, seq, aggregate_type, aggregate_id, event_type, event_version,
                  payload, traceparent, tracestate, occurred_at
        """,
        {
            "aggregate_type": aggregate_type,
            "aggregate_id": str(aggregate_id),
            "event_type": event_type,
            "event_version": event_version,
            "payload": Json(payload, dumps=lambda v: json.dumps(v, default=_json_default)),
            "traceparent": carrier.get("traceparent"),
            "tracestate": carrier.get("tracestate"),
        },
    )
    return cur.fetchone()


# ============================================================================================
# The relay (Week 3, D39) — publishes unpublished rows to Kafka, in-process per service.
#
# In-process rather than a sidecar container: a separate relay process would need
# `sfo_order_core`/`sfo_payment_core` write credentials in a second place, and be a second
# process able to write that service's database — a real boundary regression under D01/D19
# for no gain at this platform's size. It also gives one relay per table for free, which is
# what buys per-key ordering (a second relay racing this one over the same table could
# publish out of `seq` order).
# ============================================================================================

import asyncio
from contextlib import asynccontextmanager
from logging import Logger
from uuid import UUID

from aiokafka.errors import KafkaError
from fastapi import FastAPI
from prometheus_client import Counter, Gauge

from common.kafka import SchemaRegistryValidator, build_producer, inject_trace_headers
from common.postgres import PostgresPool

# Declared once at module level, not per relay instance — two relays (order's and
# payment's) share this process' default CollectorRegistry, and constructing a second
# `Gauge("sfo_outbox_backlog", ...)` with the same name would raise
# `Duplicated timeseries in CollectorRegistry`. `.labels(outbox_table=...)` per instance is
# what distinguishes them on a shared metric instead.
_OUTBOX_BACKLOG = Gauge(
    "sfo_outbox_backlog", "Unpublished rows waiting on the relay", ["outbox_table"]
)
_OUTBOX_LAG_SECONDS = Gauge(
    "sfo_outbox_lag_seconds", "Age in seconds of the oldest unpublished row", ["outbox_table"]
)
_OUTBOX_PUBLISHED_TOTAL = Counter(
    "sfo_outbox_published_total", "Rows successfully published", ["outbox_table"]
)
_OUTBOX_PUBLISH_FAILURES_TOTAL = Counter(
    "sfo_outbox_publish_failures_total", "Publish attempts that failed", ["outbox_table"]
)

_CLAIM_BATCH = """
    UPDATE {table} o
       SET attempts = o.attempts + 1
      FROM (SELECT id FROM {table}
             WHERE published_at IS NULL
             ORDER BY seq
             LIMIT %(batch_size)s
               FOR UPDATE SKIP LOCKED) c
     WHERE o.id = c.id
    RETURNING o.id, o.seq, o.aggregate_id, o.event_type, o.event_version, o.payload,
              o.traceparent, o.tracestate, o.occurred_at
"""

# `%(ids)s::uuid[]` — psycopg2 adapts a Python list to a text[] literal, and Postgres will
# not implicitly cast a whole array parameter to compare against a uuid column (unlike a
# scalar %s::uuid, which does cast implicitly). Found by running this for real: the relay
# connected to Kafka, published successfully, then failed at the very next step with
# "operator does not exist: uuid = text" on every batch, forever — outbox rows shows the
# right `attempts` count but no `published_at`, and any batch matched by the missing-row
# check on the next claim gets republished, which is real work masking an actual bug.
_MARK_PUBLISHED = (
    "UPDATE {table} SET published_at = CURRENT_TIMESTAMP WHERE id = ANY(%(ids)s::uuid[])"
)

_MARK_FAILED = "UPDATE {table} SET last_error = %(error)s WHERE id = ANY(%(ids)s::uuid[])"

_OLDEST_UNPUBLISHED_AGE = """
    SELECT extract(epoch FROM CURRENT_TIMESTAMP - min(occurred_at)) AS age_seconds,
           count(*) AS backlog
      FROM {table}
     WHERE published_at IS NULL
"""

_PRUNE_PUBLISHED = "DELETE FROM {table} WHERE published_at < CURRENT_TIMESTAMP - INTERVAL '7 days'"


class OutboxRelay:
    """One relay, bound to one outbox table in one service's own database.

    `producer` (Service Name):  Kafka's `producer` field on the wire; distinct from
    `aggregate_type`, which the caller already fixed when it called `append_outbox`.
    """

    def __init__(
        self,
        db: PostgresPool,
        *,
        table: str,
        topic: str,
        bootstrap_servers: str,
        producer_name: str,
        logger: Logger,
        schema_registry_url: str = "http://schema-registry:8081",
        batch_size: int = 50,
        poll_interval: float = 1.0,
        prune_every: int = 300,
    ):
        self._db = db
        self._table = table
        self._topic = topic
        self._bootstrap_servers = bootstrap_servers
        self._producer_name = producer_name
        self._logger = logger
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._prune_every = prune_every
        self._kafka_producer = None
        self._schema_validator = SchemaRegistryValidator(schema_registry_url)
        self._task: asyncio.Task | None = None
        self._registration_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._backlog = _OUTBOX_BACKLOG.labels(outbox_table=table)
        self._lag = _OUTBOX_LAG_SECONDS.labels(outbox_table=table)
        self._published_total = _OUTBOX_PUBLISHED_TOTAL.labels(outbox_table=table)
        self._failures_total = _OUTBOX_PUBLISH_FAILURES_TOTAL.labels(outbox_table=table)

    @asynccontextmanager
    async def lifespan(self, _: FastAPI = None):
        """Compose alongside `PostgresPool.lifespan` via `common.lifespan.compose_lifespan`.

        Constructing the `AIOKafkaProducer` happens inside the running loop, on the first
        poll iteration, never here and never at import — `aiokafka` requires a running
        event loop to construct correctly. Startup failure (Kafka unreachable) is
        non-fatal, matching `TemporalGateway.lifespan`'s established pattern: the request
        path must stay up while a dependency is down, so outbox rows simply accumulate
        until the broker returns.
        """
        # Fired as a background task, not awaited — `register_all` retries internally for
        # up to ~15s per its own docstring, and this service's own startup must not wait on
        # a dependency it does not need to serve traffic or run the relay loop. Awaiting it
        # here is what caused the registry to come up permanently empty the first time this
        # ran for real: schema-registry was not yet resolvable, the one-shot attempt (before
        # the retry was added) failed immediately, and nothing ever tried again.
        self._registration_task = asyncio.create_task(
            self._schema_validator.register_all(), name=f"outbox-schema-registration-{self._table}"
        )
        self._task = asyncio.create_task(self._run(), name=f"outbox-relay-{self._table}")
        try:
            yield
        finally:
            self._stopping.set()
            self._task.cancel()
            self._registration_task.cancel()
            for task in (self._task, self._registration_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if self._kafka_producer is not None:
                await self._kafka_producer.stop()

    async def _ensure_producer(self) -> bool:
        if self._kafka_producer is not None:
            return True
        try:
            producer = build_producer(self._bootstrap_servers)
            await producer.start()
        except KafkaError as exc:
            self._logger.warning(
                "Outbox relay (%s): Kafka unreachable, will retry: %s", self._table, exc
            )
            return False
        self._kafka_producer = producer
        self._logger.info("Outbox relay (%s): producer connected", self._table)
        return True

    async def _run(self) -> None:
        iteration = 0
        while not self._stopping.is_set():
            iteration += 1
            rows: list[dict] = []
            try:
                if not await self._ensure_producer():
                    await asyncio.sleep(self._poll_interval)
                    continue

                rows = await asyncio.to_thread(self._claim_batch)
                if not rows:
                    await asyncio.to_thread(self._update_gauges)
                    if iteration % self._prune_every == 0:
                        await asyncio.to_thread(self._prune)
                    await asyncio.sleep(self._poll_interval)
                    continue

                published_ids = []
                for row in rows:  # already ordered by seq — publish in that order
                    await self._publish_one(row)
                    published_ids.append(row["id"])
                await self._kafka_producer.flush()
                await asyncio.to_thread(self._mark_published, published_ids)
                self._published_total.inc(len(published_ids))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the relay must never crash the process
                self._logger.error(
                    "Outbox relay (%s) iteration failed: %s", self._table, exc
                )
                self._failures_total.inc()
                # Best-effort: record why on whatever rows this iteration had claimed, so
                # `last_error` is visible to an operator inspecting a stuck row directly —
                # `attempts` alone (bumped by the claim query itself) says a row is stuck,
                # not why. Never let this secondary write mask the original failure.
                if rows:
                    try:
                        await asyncio.to_thread(
                            self._mark_failed, [row["id"] for row in rows], str(exc)
                        )
                    except Exception as mark_exc:  # noqa: BLE001
                        self._logger.error(
                            "Outbox relay (%s): could not record last_error: %s",
                            self._table, mark_exc,
                        )
                # A broker-side failure invalidates the producer's internal state under
                # idempotence; rebuild it next iteration rather than reuse a producer that
                # may be in an unknown state.
                if isinstance(exc, KafkaError) and self._kafka_producer is not None:
                    await self._kafka_producer.stop()
                    self._kafka_producer = None
                await asyncio.sleep(self._poll_interval)

    async def _publish_one(self, row: dict) -> None:
        # Validated against the schema for THIS row's own declared version — see
        # SchemaRegistryValidator's docstring for why that is version-keyed rather than
        # "whatever the registry currently holds". A failure here means this process's own
        # `append_outbox` call wrote something that disagrees with its own event contract
        # in `common/events/` — a real bug, not a poison message from elsewhere, so it is
        # left for the outer loop's retry-with-backoff rather than special-cased: the same
        # row will fail identically next time until the code is fixed, which is the
        # intended, visible failure mode for an internal contract violation.
        self._schema_validator.validate(row["event_type"], row["event_version"], row["payload"])
        envelope = {
            "event_id": str(row["id"]),
            "event_type": row["event_type"],
            "event_version": row["event_version"],
            "aggregate_type": self._table.replace("_outbox", ""),
            "aggregate_id": str(row["aggregate_id"]),
            "seq": row["seq"],
            "occurred_at": row["occurred_at"].isoformat(),
            "producer": self._producer_name,
            "data": row["payload"],
        }
        headers = inject_trace_headers(row.get("traceparent"), row.get("tracestate"))
        await self._kafka_producer.send_and_wait(
            self._topic,
            key=str(row["aggregate_id"]).encode("utf-8"),
            value=_encode(envelope),
            headers=headers,
        )

    # --- blocking psycopg2 work, run via asyncio.to_thread so it never touches the event
    # loop directly. Never touches the request-serving pool's budget beyond one leased
    # connection for the duration of a claim, same as every other repository call. ---

    def _claim_batch(self) -> list[dict]:
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                _CLAIM_BATCH.format(table=self._table), {"batch_size": self._batch_size}
            )
            return cur.fetchall()

    def _mark_published(self, ids: list[UUID]) -> None:
        with self._db.cursor(commit=True) as cur:
            cur.execute(_MARK_PUBLISHED.format(table=self._table), {"ids": [str(i) for i in ids]})

    def _mark_failed(self, ids: list[UUID], error: str) -> None:
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                _MARK_FAILED.format(table=self._table),
                {"ids": [str(i) for i in ids], "error": error[:2000]},
            )

    def _update_gauges(self) -> None:
        with self._db.cursor() as cur:
            cur.execute(_OLDEST_UNPUBLISHED_AGE.format(table=self._table))
            row = cur.fetchone()
        self._backlog.set(row["backlog"] or 0)
        self._lag.set(row["age_seconds"] or 0)

    def _prune(self) -> None:
        with self._db.cursor(commit=True) as cur:
            cur.execute(_PRUNE_PUBLISHED.format(table=self._table))


def _encode(envelope: dict) -> bytes:
    return json.dumps(envelope, default=_json_default).encode("utf-8")
