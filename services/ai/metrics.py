"""Prometheus metrics for the AI Service (Week 4, D59).

Declared once at module scope, never inside a handler or message loop — the same rule
`analytics/consumer.py` states for its own counters: a metric constructed per call raises
`Duplicated timeseries` on the second one.
"""

from prometheus_client import Counter, Gauge, Histogram

INGESTION_EVENTS_TOTAL = Counter(
    "sfo_ai_ingestion_events_total",
    "menu.published events handled by the ingestion worker, by outcome",
    ["outcome"],  # ingested | skipped | removed | duplicate | dlq
)
DOCUMENTS_INDEXED = Gauge(
    "sfo_ai_documents_indexed",
    "Rows currently in the vector store, by document kind",
    ["kind"],  # menu_item | restaurant
)
EMBEDDING_LATENCY_SECONDS = Histogram(
    "sfo_ai_embedding_latency_seconds",
    "Time to embed one batch of texts",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
SEARCH_REQUESTS_TOTAL = Counter(
    "sfo_ai_search_requests_total",
    "POST /api/v1/ai/search requests, by outcome",
    ["outcome"],  # ok | empty | error
)
SEARCH_LATENCY_SECONDS = Histogram(
    "sfo_ai_search_latency_seconds",
    "End-to-end time to answer a search (embed + vector query)",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
RAG_REQUESTS_TOTAL = Counter(
    "sfo_ai_rag_requests_total",
    "POST /api/v1/ai/rag-context requests, by outcome",
    ["outcome"],  # complete | degraded | error
)
RAG_LATENCY_SECONDS = Histogram(
    "sfo_ai_rag_latency_seconds",
    "End-to-end time to assemble a RAG context",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
RAG_SOURCE_FAILURES_TOTAL = Counter(
    "sfo_ai_rag_source_failures_total",
    "RAG sources that errored or ran out of their time budget — the retrieval health signal",
    ["source"],  # vector | analytics | courier
)
INGESTION_LAST_EVENT_SECONDS = Gauge(
    "sfo_ai_ingestion_last_event_timestamp_seconds",
    "Unix time of the last message the ingestion worker processed — a flat line means a wedged worker",
)
