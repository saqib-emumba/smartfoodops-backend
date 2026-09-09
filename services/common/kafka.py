"""Kafka transport: the producer the relay uses, the schema-registry validation both sides
share, and W3C trace-context helpers for carrying a trace across the async boundary
(Week 3, D39/D40).

Imported on demand only, never from `common/__init__.py` — `aiokafka` and `fastjsonschema`
are not chassis dependencies (see `common/requirements.txt`'s "deliberately NOT here" list),
only order, payment, analytics and notification install them. A service that never touches
Kafka never imports this module and pays nothing for it.
"""

import asyncio
import logging
from typing import Any

import httpx
from aiokafka import AIOKafkaProducer
from opentelemetry import context as otel_context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from common.events.envelope import EventEnvelope
from common.events.order import EVENT_DATA_MODELS as ORDER_EVENT_MODELS
from common.events.payment import EVENT_DATA_MODELS as PAYMENT_EVENT_MODELS

_ALL_EVENT_MODELS: dict[str, tuple[type, int]] = {**ORDER_EVENT_MODELS, **PAYMENT_EVENT_MODELS}

_propagator = TraceContextTextMapPropagator()

logger = logging.getLogger("common.kafka")


def build_producer(bootstrap_servers: str) -> AIOKafkaProducer:
    """One producer configuration, shared by every relay.

    `acks="all"` + `enable_idempotence=True` is the whole ballgame: without both, a leader
    failover between retries can reorder or duplicate a message *at the broker*, and no
    amount of consumer-side dedup can undo that — the ordering guarantee has to hold before
    the message is ever acked. No transactional producer: the outbox table already gives
    atomicity with Postgres (D39), and a Kafka transaction cannot span a database it has no
    idea exists.
    """
    return AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        acks="all",
        enable_idempotence=True,
        compression_type="gzip",
        linger_ms=5,
    )


def inject_trace_headers(traceparent: str | None, tracestate: str | None) -> list[tuple[str, bytes]]:
    """Kafka headers carrying whatever trace context was captured at outbox-append time.

    Not the relay's own ambient context — the relay runs asynchronously, potentially
    minutes after the request that caused the write, and injecting its context here would
    start a trace disconnected from the one the event actually belongs to. The stored
    `traceparent`/`tracestate` (captured by `common/outbox.py::append_outbox`, itself
    called inside the request's span) is what preserves the link.
    """
    headers = []
    if traceparent:
        headers.append(("traceparent", traceparent.encode("utf-8")))
    if tracestate:
        headers.append(("tracestate", tracestate.encode("utf-8")))
    return headers


def extract_trace_context(headers: list[tuple[str, bytes]] | None) -> otel_context.Context:
    """The inverse, on the consumer side: rebuild a Context from Kafka message headers so
    a CONSUMER-kind span parents correctly under the original request's trace instead of
    starting a new, orphaned one.
    """
    carrier = {}
    for key, value in headers or []:
        if key in ("traceparent", "tracestate"):
            carrier[key] = value.decode("utf-8")
    return _propagator.extract(carrier)


class SchemaRegistryValidator:
    """Fetches and caches JSON Schemas from the registry, keyed by `(event_type,
    event_version)` — not by "whatever is currently registered for this topic". That
    distinction is what makes a rolling deploy safe: an old producer pod still emitting
    `event_version: 1` and a new one emitting `version: 2` are each validated against their
    own declared version, never against whichever version a shared cache happened to fetch
    most recently.

    Registration (`register_all`) is a startup-time, best-effort step: it POSTs each
    event type's `model_json_schema()` so the registry can enforce compatibility between
    versions. Validation (`validate`) is the hot-path step, run on every produce and every
    consume, and uses `fastjsonschema` — compiled once per `(event_type, version)` pair and
    cached, so there is no per-message network round trip.
    """

    def __init__(self, registry_url: str):
        self._registry_url = registry_url.rstrip("/")
        self._validators: dict[tuple[str, int], Any] = {}

    async def register_all(self, *, attempts: int = 5, retry_delay: float = 3.0) -> None:
        """Register every known event type's current schema. Logged, never raised — a
        registry that is down at startup must not take a producer down with it; the
        registry is a compatibility aid, not a dependency this platform's checkout path
        can afford to have (see D38: nothing here may sit on the critical path).

        Retries each subject up to `attempts` times before giving up on it — found
        necessary the first time this ran for real: on a fresh `docker compose up`,
        schema-registry's hostname was not yet resolvable when order-service and
        payment-service each tried once, so every registration failed and the registry
        came up permanently empty even though nothing was actually wrong. A one-shot
        attempt is indistinguishable from "the registry will never be reachable"; a few
        retries a few seconds apart is what a normal startup race actually needs. Callers
        should schedule this as a background task rather than await it inline — see
        `OutboxRelay.lifespan` — so a slow registry cannot add its own latency to this
        service's own startup.
        """
        import fastjsonschema  # local import: optional dependency, see module docstring

        async with httpx.AsyncClient(timeout=5.0) as client:
            for event_type, (model, version) in _ALL_EVENT_MODELS.items():
                schema = model.model_json_schema()
                subject = f"{event_type}-value"
                for attempt in range(1, attempts + 1):
                    try:
                        resp = await client.post(
                            f"{self._registry_url}/subjects/{subject}/versions",
                            json={"schemaType": "JSON", "schema": _dump_schema(schema)},
                        )
                        if resp.status_code >= 300:
                            logger.warning(
                                "Schema Registry rejected %s v%d (attempt %d/%d): %s %s",
                                event_type, version, attempt, attempts,
                                resp.status_code, resp.text,
                            )
                        else:
                            break  # registered; stop retrying this subject
                    except httpx.HTTPError as exc:
                        logger.warning(
                            "Could not register schema for %s v%d (attempt %d/%d): %s",
                            event_type, version, attempt, attempts, exc,
                        )
                    if attempt < attempts:
                        await asyncio.sleep(retry_delay)
                # Cache the compiled validator locally regardless of whether registration
                # ever succeeded — the registry's copy is for cross-version compatibility
                # checking; this process validates against its own model either way.
                self._compile_and_cache(event_type, version, schema)

    def _compile_and_cache(self, event_type: str, version: int, schema: dict) -> Any:
        import fastjsonschema

        validator = fastjsonschema.compile(schema)
        self._validators[(event_type, version)] = validator
        return validator

    def validate(self, event_type: str, version: int, data: dict) -> None:
        """Raise `fastjsonschema.JsonSchemaException` if `data` does not match the schema
        registered for this exact `(event_type, version)` pair. A cache miss compiles the
        local model's current schema on demand — this process may not have called
        `register_all` (a consumer-only process, say), and the schema for any version this
        platform's own code emits is always derivable locally from `common/events/`.
        """
        key = (event_type, version)
        validator = self._validators.get(key)
        if validator is None:
            model, _ = _ALL_EVENT_MODELS.get(event_type, (None, None))
            if model is None:
                return  # unknown event_type: not this validator's job to reject (see consumer rule below)
            validator = self._compile_and_cache(event_type, version, model.model_json_schema())
        validator(data)


def _dump_schema(schema: dict) -> str:
    import json

    return json.dumps(schema)


__all__ = [
    "build_producer",
    "inject_trace_headers",
    "extract_trace_context",
    "SchemaRegistryValidator",
    "EventEnvelope",
]
