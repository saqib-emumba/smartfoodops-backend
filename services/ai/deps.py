"""Process-lifetime singletons for the AI Service.

Here rather than in main.py for the same reason every other service's deps.py exists —
main.py imports the routers in order to include them, so a router importing its
collaborators from main.py would be a cycle.

Shared by two processes: the FastAPI app and the ingestion worker both import this module,
so both get the same pool, embedder and sibling clients.
"""

from common.bootstrap import bootstrap
from ai.embeddings import build_embedder

runtime = bootstrap(
    "ai-service",
    exhausted_detail="Database connection pool exhausted",
)

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

# Built at import: loading the model is the slowest part of startup, and doing it here means
# a container that cannot load it fails immediately instead of on the first request.
embedder = build_embedder()
