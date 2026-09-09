"""Process-lifetime singletons for the Order Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from order import deps` and reference `deps.orders`, not
`from order.deps import orders`: the latter binds at import time and makes any future swap
impossible without touching every router.

A Temporal client again, as of D47. D36 removed the one this service held before, because
naming a workflow by class reference dragged the orchestrator's whole import graph in with
it; `common.temporal.SagaClient` names workflows by string instead, so the client comes back
without the imports that made it a problem. `orchestrator_service` below is now a thin
wrapper over that rather than an HTTP client — same name, same methods, same callers.

`outbox_relay` is Week 3's addition (D39): it publishes `order_outbox` rows to Kafka in the
background. Its own lifespan is composed alongside `db.lifespan` in main.py — see that
file's comment on why one relay per table, in-process, was chosen over a sidecar.
"""

from common.bootstrap import bootstrap
from common.config import DEFAULT_TEMPORAL_ADDRESS, service_url
from common.events.topics import ORDER_EVENTS_TOPIC
from common.outbox import OutboxRelay
from common.temporal import SagaClient, TemporalGateway
from order.clients.menu import MenuServiceClient
from order.clients.orchestrator import OrchestratorClient
from order.clients.restaurant import RestaurantServiceClient
from order.clients.user import UserServiceClient
from order.repositories.orders import OrderRepository
from order.repositories.tracking import OrderTrackingRepository

TEMPORAL_ADDRESS = service_url("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)

runtime = bootstrap(
    "order-service",
    exhausted_detail="Database connection pool exhausted; order was not created",
)

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

orders = OrderRepository(db, logger=logger, service_name=SERVICE_NAME)
tracking = OrderTrackingRepository(db)
menu_service = MenuServiceClient(logger)
user_service = UserServiceClient(logger)
restaurant_service = RestaurantServiceClient(logger)

temporal = TemporalGateway(TEMPORAL_ADDRESS, logger=logger)
orchestrator_service = OrchestratorClient(SagaClient(temporal, logger=logger), logger=logger)

outbox_relay = OutboxRelay(
    db,
    table="order_outbox",
    topic=ORDER_EVENTS_TOPIC,
    bootstrap_servers=service_url("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
    schema_registry_url=service_url("SCHEMA_REGISTRY_URL", "http://schema-registry:8081"),
    producer_name=SERVICE_NAME,
    logger=logger,
)
