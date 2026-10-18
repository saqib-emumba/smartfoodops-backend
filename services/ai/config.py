"""Settings specific to the AI Service (Week 4, D59).

Read from the environment with defaults, because none of these is a secret: the only
credential this service can hold is an OpenAI key, and that is read with `required()` at the
one place it is used (`embeddings.OpenAIEmbedder`), so a deployment that never selects that
provider never needs one.
"""

import os

# The FastAPI app and the ingestion worker share this package and `deps.py`; each names itself
# so logs, traces and the Prometheus `job` label tell the two processes apart.
PROCESS_NAME = os.getenv("AI_PROCESS_NAME", "ai-service")

# "local" runs all-MiniLM-L6-v2 in-process (no network, no key, no per-call cost); "openai"
# calls the embeddings API. Selected by env so switching is a deploy, not a code change.
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local")

# The vector columns in db/vector/init.sql are `vector(384)`. A provider must emit exactly
# this many dimensions, and the OpenAI embedder asks the API to shorten its vectors to it —
# so changing the width is a schema change and a re-ingest, never an accident.
EMBEDDING_DIMENSIONS = 384

LOCAL_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

# How many nearest-neighbour candidates an HNSW scan keeps while a WHERE clause filters
# them. The default (40) can leave a filtered query short of `top_k` rows; see
# repositories/vectors.py for why this is raised per query rather than globally.
HNSW_EF_SEARCH = 100

# How long the RAG assembler waits on each source before giving up on it (D62). Tight on purpose:
# the caller is a language model's prompt being built, and an answer that is one source short
# but arrives now beats a complete one that arrives after the user has given up. A source that
# misses its budget is reported in `sources_failed`, never silently dropped.
RAG_VECTOR_TIMEOUT_SECONDS = 5.0
RAG_ANALYTICS_TIMEOUT_SECONDS = 3.0
RAG_RIDER_TIMEOUT_SECONDS = 3.0

# How many restaurants courier availability is looked up for. One rider-service call per
# restaurant, so this bounds the fan-out however many dishes matched.
RAG_MAX_COURIER_LOOKUPS = 5

# Ingestion consumer group and metrics port (a bare prometheus_client server, the same
# approach notification-consumer takes — the worker is not a FastAPI process).
INGESTION_CONSUMER_GROUP = "ai-ingestion"
INGESTION_METRICS_PORT = 9111

# --- Week 5: the chat model (D63) ---
# "anthropic" calls Claude through the API and needs ANTHROPIC_API_KEY, read with `required()`
# in `llm.provider.build_chat_model` and nowhere else; "ollama" talks to a self-hosted Ollama server, so
# nothing leaves the deployment. Selected by env, like EMBEDDING_PROVIDER.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")

# Low on purpose for Ollama: the assistant restates retrieved menu data, it does not write fiction. Not sent to
# Anthropic, whose current model rejects a non-default temperature (see llm.py).
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))

# --- Week 5: the assistant (D63) ---
# Conversation memory lives in Redis (db 3, beside the menu cache on 0, auth on 1, riders on 2),
# keyed per user, with a TTL: bounded, survives a restart, and shared by every replica — which an
# in-process checkpointer is not. A Redis outage means "no history", never a failed answer.
AI_REDIS_URL = os.getenv("AI_REDIS_URL", "redis://cache-redis:6379/3")
CONVERSATION_TTL_SECONDS = 3600
# Messages kept per conversation (a question and its answer are two).
CONVERSATION_MAX_MESSAGES = 12

# Distinct dishes handed to the model per question. Fewer than the search API's ceiling: every dish is
# prompt tokens, and a recommendation list nobody reads past five is not worth the latency.
ASSISTANT_TOP_K = 6
# How many candidates retrieval fetches before identical-looking ones are dropped and the best ASSISTANT_TOP_K
# distinct ones kept: duplicates would otherwise crowd the context (assistant/agents/context.py `_distinct`).
ASSISTANT_CANDIDATES = 18
# Cosine floor below which a match is treated as "nothing matches" (the Phase 4 open point). A
# guess calibrated on the seed menus with all-MiniLM-L6-v2, where on-topic queries score well
# above it; revisit if the embedding model changes (D59).
ASSISTANT_MIN_SCORE = float(os.getenv("ASSISTANT_MIN_SCORE", "0.30"))
# Longest question the assistant accepts: the question goes into a prompt.
ASSISTANT_MAX_QUESTION_CHARS = 500

# Recent commented reviews handed to the summariser; the count and average always cover all.
REVIEW_SUMMARY_MAX_REVIEWS = 30

# An order counts as "after an AI interaction" if the same customer places it at a restaurant the
# assistant named to them within this many hours. A convention, not a measurement of cause: someone
# who was already going to order from that restaurant is counted too, which is why the dashboard calls
# it attribution and not lift.
ATTRIBUTION_WINDOW_HOURS = int(os.getenv("ATTRIBUTION_WINDOW_HOURS", "24"))
ATTRIBUTION_CONSUMER_GROUP = "ai-attribution"
