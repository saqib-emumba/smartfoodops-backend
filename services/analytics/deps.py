"""Process-lifetime singletons for the Analytics Service.

Here rather than in main.py for the same reason every other service's deps.py exists —
main.py imports the routers in order to include them, so a router importing its
collaborators from main.py would be a cycle.

No sibling clients: this service reaches no other service over HTTP. Every fact it has
came from Kafka.
"""

from common.bootstrap import bootstrap
from common.config import service_url
from common.events.topics import ORDER_EVENTS_TOPIC
from analytics.consumer import AnalyticsConsumer
from analytics.repositories.projections import ProjectionsRepository

runtime = bootstrap(
    "analytics-service",
    exhausted_detail="Database connection pool exhausted",
)

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

projections = ProjectionsRepository(db, logger=logger)

consumer = AnalyticsConsumer(
    projections,
    topic=ORDER_EVENTS_TOPIC,
    bootstrap_servers=service_url("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
    schema_registry_url=service_url("SCHEMA_REGISTRY_URL", "http://schema-registry:8081"),
    logger=logger,
)
