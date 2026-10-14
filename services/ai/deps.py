"""Process-lifetime singletons for the AI Service.

Here rather than in main.py for the same reason every other service's deps.py exists —
main.py imports the routers in order to include them, so a router importing its
collaborators from main.py would be a cycle.

Shared by two processes: the FastAPI app and the ingestion worker both import this module,
so both get the same pool, embedder and sibling clients.
"""

from pgvector.psycopg2 import register_vector

from common.bootstrap import bootstrap
from ai import config
from ai.clients.analytics import AnalyticsServiceClient
from ai.clients.rider import RiderServiceClient
from ai.embeddings import build_embedder
from ai.rag_context import RagContextAssembler
from ai.repositories.vectors import VectorRepository

runtime = bootstrap(
    config.PROCESS_NAME,
    exhausted_detail="Database connection pool exhausted",
)

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

# Built at import: loading the model is the slowest part of startup, and doing it here means
# a container that cannot load it fails immediately instead of on the first request.
embedder = build_embedder()

vectors = VectorRepository(db, logger=logger)
rag = RagContextAssembler(
    vectors,
    embedder,
    AnalyticsServiceClient(logger),
    RiderServiceClient(logger),
    logger=logger,
)


def register_vector_types() -> None:
    """Teach psycopg2 the `vector` type, for every connection in this process.

    Must run after the pool is open (it queries `pg_type` for the type's OID, which needs a
    connection) and before the first query that passes or returns an embedding. `globally=True`
    registers the type once for the process rather than per connection: the OID is the same for
    every connection to this database, so there is no reason to repeat the lookup on each lease
    from the pool — and it means no change to the shared `PostgresPool` in `common`.

    Raises if the `vector` extension is missing from the database, which is the right failure:
    this service cannot do anything useful without it, and it should stop at startup rather than
    on the first search.
    """
    with db.cursor() as cur:
        register_vector(cur, globally=True)
    logger.info("pgvector types registered")
