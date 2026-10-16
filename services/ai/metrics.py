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

# --- Week 5: the assistant, order explanations and partner tools (D63, D64) ---
ASSISTANT_REQUESTS_TOTAL = Counter(
    "sfo_ai_assistant_requests_total",
    "Assistant questions, by routed intent and how they ended",
    # outcome: answered (grounded model answer) | fallback (guardrail replaced the model's answer)
    #          | no_results (nothing cleared the similarity floor) | refused (out of scope)
    #          | cancelled (the client disconnected mid-stream) | error
    ["intent", "outcome"],
)
ASSISTANT_LATENCY_SECONDS = Histogram(
    "sfo_ai_assistant_latency_seconds",
    "Time from question received to the stream's final chunk",
    buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
)
ASSISTANT_FIRST_TOKEN_SECONDS = Histogram(
    "sfo_ai_assistant_first_token_seconds",
    "Time from question received to the first streamed token — what the user feels",
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 15),
)
LLM_CALLS_TOTAL = Counter(
    "sfo_ai_llm_calls_total",
    "Chat-model calls, by graph node and outcome",
    ["node", "outcome"],  # outcome: ok | error
)
LLM_FAILURES_TOTAL = Counter(
    "sfo_ai_llm_failures_total",
    "Chat-model failures by kind — the provider health signal",
    ["kind"],  # timeout | rate_limit | auth | unavailable | other
)
GUARDRAIL_VIOLATIONS_TOTAL = Counter(
    "sfo_ai_guardrail_violations_total",
    "Model answers the grounding check rejected, by what it caught",
    ["kind"],  # unknown_name | unknown_price | unknown_quantity
)
RECOMMENDED_ITEMS_TOTAL = Counter(
    "sfo_ai_recommended_items_total",
    "Dishes named in grounded answers — the denominator of the recommendation acceptance rate",
)
EXPLAIN_REQUESTS_TOTAL = Counter(
    "sfo_ai_explain_requests_total",
    "POST /api/v1/ai/explain-order requests, by outcome",
    ["outcome"],  # answered | fallback | error
)
PARTNER_REQUESTS_TOTAL = Counter(
    "sfo_ai_partner_requests_total",
    "Restaurant-admin authoring requests, by tool and outcome",
    ["tool", "outcome"],  # tool: describe_dish | promo | review_summary; outcome: ok | empty | error
)

# --- Week 5: conversion attribution (D63) ---
RECOMMENDING_INTERACTIONS_TOTAL = Counter(
    "sfo_ai_recommending_interactions_total",
    "Assistant answers that named at least one restaurant — the denominator of the acceptance rate",
)
ATTRIBUTED_ORDERS_TOTAL = Counter(
    "sfo_ai_attributed_orders_total",
    "Orders placed at a restaurant the assistant recommended to that customer within the window",
)
ATTRIBUTED_REVENUE_TOTAL = Counter(
    "sfo_ai_attributed_revenue_total",
    "Order value of attributed orders — the assistant's commercial return",
)
ATTRIBUTION_EVENTS_TOTAL = Counter(
    "sfo_ai_attribution_events_total",
    "order.created events seen by the attribution consumer, by outcome",
    ["outcome"],  # attributed | no_match | malformed
)
