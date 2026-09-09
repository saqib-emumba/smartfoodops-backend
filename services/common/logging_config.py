"""Uniform logging setup.

Every service emits records in the same shape so that logs from the whole compose stack
can be read — and later shipped — as one stream.

`configure_logging`'s signature and return type are unchanged from Week 1: still
`(service_name: str) -> logging.Logger`, still called first by every process in
`bootstrap()`. What is new is that every record now carries the active OpenTelemetry trace
and span id, so a log line and a Jaeger trace can be joined by eye or by query.

A `LogRecordFactory` does that, not a `logging.Filter`: a filter attached to one handler
only decorates records that reach *that* handler, and any record formatted by a different
handler raises `ValueError: Formatting field not found` and is lost to stderr instead of
printed. A record factory decorates every record at creation, before any handler sees it,
so there is nothing a second handler could be missing.
"""

import logging

from opentelemetry import trace

LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[trace=%(otel_trace_id)s span=%(otel_span_id)s] %(message)s"
)

_factory_installed = False


def _install_trace_context_factory() -> None:
    """Chain a LogRecordFactory that stamps otel_trace_id/otel_span_id onto every record.

    Idempotent and chains off whatever factory is already installed, rather than replacing
    it outright — the orchestrator image runs two entrypoints from one codebase, and a
    second `configure_logging` call in the same process must not double-wrap.
    """
    global _factory_installed
    if _factory_installed:
        return

    previous_factory = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        record = previous_factory(*args, **kwargs)
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            # Zero-padded to the full width (032x / 016x) — an unpadded hex id with
            # leading zeros produces a short string that will not join against what
            # Jaeger or any other W3C-trace-context-aware tool renders.
            record.otel_trace_id = format(span_context.trace_id, "032x")
            record.otel_span_id = format(span_context.span_id, "016x")
        else:
            record.otel_trace_id = "-"
            record.otel_span_id = "-"
        return record

    logging.setLogRecordFactory(_factory)
    _factory_installed = True


def configure_logging(service_name: str) -> logging.Logger:
    """Initialise root logging and return the logger this service should use."""
    _install_trace_context_factory()
    # `force=True`: `basicConfig` is a documented no-op once the root logger already has a
    # handler. That happens to be harmless today only because uvicorn configures its own
    # named loggers (`uvicorn`, `uvicorn.error`, `uvicorn.access`) and leaves root alone —
    # an accident of ordering, not a guarantee. `force=True` removes and closes any existing
    # root handlers before installing this one, so a future regression here is a visible
    # format change, not a silently-dropped one.
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, force=True)
    return logging.getLogger(service_name)
