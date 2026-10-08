"""Process-lifetime singletons for the Menu Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from menu import deps` and reference `deps.menus`, not
`from menu.deps import menus`: the latter binds at import time.
"""

from common.bootstrap import bootstrap
from common.config import DEFAULT_REDIS_URL, service_url
from common.kafka import KafkaGateway
from menu.clients.restaurant import RestaurantServiceClient
from menu.repositories.cache import MenuCache
from menu.repositories.menus import MenuRepository

REDIS_URL = service_url("REDIS_URL", DEFAULT_REDIS_URL)

runtime = bootstrap(
    "menu-service",
    exhausted_detail="Database connection pool exhausted; the menu could not be served",
)

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

menus = MenuRepository(db)
cache = MenuCache(REDIS_URL, logger=logger)
restaurant_service = RestaurantServiceClient(logger)

# Announces a publish to the AI Service's ingestion worker (D61). Best-effort by design: a
# menu is committed and answered before this is attempted, and a failure is logged, not raised
# — publishing a menu must never depend on Kafka being up. The AI side reconciles on startup
# (`ai.ingestion --backfill`), so a lost event delays search freshness, never loses data.
kafka = KafkaGateway(
    bootstrap_servers=service_url("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
    producer_name="menu-service",
    logger=logger,
    schema_registry_url=service_url("SCHEMA_REGISTRY_URL", "http://schema-registry:8081"),
)
