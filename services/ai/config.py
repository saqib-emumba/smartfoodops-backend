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

# Ingestion consumer group and metrics port (a bare prometheus_client server, the same
# approach notification-consumer takes — the worker is not a FastAPI process).
INGESTION_CONSUMER_GROUP = "ai-ingestion"
INGESTION_METRICS_PORT = 9111
