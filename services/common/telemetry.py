"""OpenTelemetry tracing and Prometheus metrics, wired the same way for every service.

Two entry points, deliberately shaped like the chassis functions they sit beside:

  `configure_telemetry(service_name)` — process-global setup. Called from `bootstrap()`,
  right after `configure_logging`, so the trace `Resource`'s service name and the logger's
  name and `health_payload`'s `service` field can never disagree — they all come from the
  same caller. Installs the psycopg2 and httpx instrumentors here too, since both are
  process-wide monkeypatches, not something that attaches to one FastAPI `app`.

  `instrument_app(app, service_name)` — the one function that needs the `app` object,
  called the line after `install_error_handlers(app)` in every `main.py`, the same way
  that function is already the chassis's one `app`-shaped hook. Wires the FastAPI
  instrumentor, the Prometheus middleware, and the `/metrics` route.

Both are idempotent: the orchestrator image runs two entrypoints (`orchestrator.main` and
`orchestrator.worker`) built from the same code, and nothing here should double-instrument
if a test or a future composition root imports this module twice in one process.

Why programmatic and not `opentelemetry-instrument`: the auto-instrumentation agent takes
its `Resource` from `OTEL_SERVICE_NAME`, which is a second source of truth that can drift
from `bootstrap()`'s, and it cannot install `temporalio.contrib.opentelemetry.TracingInterceptor`
on the Temporal client — programmatic code is needed for the worker regardless, so one
mechanism is used everywhere rather than two.
"""

import time
from logging import Logger

from fastapi import FastAPI, Request
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from common.config import service_url

DEFAULT_OTLP_ENDPOINT = "http://jaeger:4318"

# Matches the platform's real outbound latencies (common/config.py):
# MOCK_GATEWAY_LATENCY_SECONDS=5.0, PAYMENT_HTTP_TIMEOUT=15.0. The default bucket set tops
# out at 10s, which would put every payment-gateway call in the overflow bucket.
_LATENCY_BUCKETS = (
    0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 15.0, 30.0,
)

REQUEST_COUNT = Counter(
    "sfo_http_requests_total",
    "HTTP requests handled, by matched route template",
    ["method", "route", "status"],
)
REQUEST_LATENCY = Histogram(
    "sfo_http_request_duration_seconds",
    "HTTP request latency in seconds, by matched route template",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)

_telemetry_configured = False
_app_instrumented_ids: set[int] = set()


def configure_telemetry(service_name: str, *, otlp_endpoint: str | None = None) -> None:
    """Process-global tracing setup: Resource, exporter, psycopg2 and httpx instrumentors.

    Idempotent — a second call from a process that has already configured telemetry is a
    no-op, logged at debug rather than raising, since `orchestrator.worker` and
    `orchestrator.main` share a codebase and a future test harness may import both.
    """
    global _telemetry_configured
    if _telemetry_configured:
        return

    endpoint = otlp_endpoint or service_url("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)
    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)
    # BatchSpanProcessor, never Simple: a down Jaeger must not add request latency to every
    # call this service makes. Spans queue in-process and are dropped, not retried forever,
    # if the exporter cannot reach the collector.
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Both are process-wide monkeypatches (they patch the class, not an instance), so one
    # call here covers every PostgresPool cursor and every ServiceClient call — the four
    # request methods in common/service_client.py all go through httpx.Client/AsyncClient,
    # constructed fresh per call, and the instrumentor patches the class itself.
    Psycopg2Instrumentor().instrument()

    # httpx and its instrumentor are deliberately NOT in common/requirements.txt — only
    # services that call a sibling install them (see that file's own docstring). The User
    # Service calls nobody and has neither package, so this import is lazy and optional
    # rather than a module-level import that would break the one service without it.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:
        pass
    else:
        HTTPXClientInstrumentor().instrument()

    _telemetry_configured = True


def instrument_app(app: FastAPI, service_name: str) -> None:
    """Wire one FastAPI app: server-side tracing spans, the metrics middleware, /metrics.

    Called once per app object; idempotent per `app` instance via `id(app)`, mirroring the
    process-level guard on `configure_telemetry`.
    """
    if id(app) in _app_instrumented_ids:
        return
    _app_instrumented_ids.add(id(app))

    # `excluded_urls`: without it, Prometheus's own scrape (every 10s, forever) manufactures
    # a trace per service per scrape — noise that drowns out the traces that matter.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/metrics")

    @app.middleware("http")
    async def _prometheus_metrics_middleware(request: Request, call_next):
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            # `install_error_handlers`' catch-all Exception handler sits on Starlette's
            # ServerErrorMiddleware, OUTSIDE this middleware — an unhandled exception
            # propagates THROUGH this `finally` as an exception, not a response, which is
            # exactly why `status_code` defaults to 500 above rather than being read only
            # from a `response` that may not exist.
            #
            # `request.scope["route"]` is populated by the router DURING dispatch, so it
            # must be read AFTER call_next, never before — reading it up front always
            # yields None. A route that matched nothing (a 404) leaves it unset, and the
            # fallback is a fixed label, never `request.url.path`: the whole reason this
            # middleware reads the matched template instead of the raw path is that a raw
            # path contains order UUIDs, and one series per order is an unbounded-cardinality
            # bug, not a metric.
            route = request.scope.get("route")
            route_template = getattr(route, "path", None) or "__unmatched__"
            if route_template != "/metrics":
                REQUEST_COUNT.labels(
                    method=request.method, route=route_template, status=status_code
                ).inc()
                REQUEST_LATENCY.labels(
                    method=request.method, route=route_template
                ).observe(time.perf_counter() - start)

    # Not the D35 envelope (key-decisions.md D41): Prometheus's exposition format is a
    # machine-defined content type no envelope can wrap without breaking every scraper.
    # `include_in_schema=False` keeps it off the OpenAPI docs; there is no nginx location
    # for it, so it is unreachable through the gateway by construction.
    @app.get("/metrics", include_in_schema=False)
    def _metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
