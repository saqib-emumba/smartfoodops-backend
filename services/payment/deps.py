"""Process-lifetime singletons for the Payment Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from payment import deps` and reference `deps.payments`, not
`from payment.deps import payments`: the latter binds at import time.

`outbox_relay` is Week 3's addition (D39): publishes `payment_outbox` rows to Kafka in the
background, on the same shared topic order-service's relay writes to (D40 — one topic,
keyed by `order_id`, so payment and order events for one order stay in the same partition).
"""

from common.bootstrap import bootstrap
from common.config import service_url
from common.events.topics import ORDER_EVENTS_TOPIC
from common.outbox import OutboxRelay
from payment.clients.order import OrderServiceClient
from payment.gateway import MockPaymentGateway
from payment.repositories.payments import PaymentRepository

runtime = bootstrap(
    "payment-service",
    exhausted_detail="Database connection pool exhausted; no payment was attempted",
)

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

payments = PaymentRepository(db, logger=logger)
order_service = OrderServiceClient(logger)
gateway = MockPaymentGateway(logger)

outbox_relay = OutboxRelay(
    db,
    table="payment_outbox",
    topic=ORDER_EVENTS_TOPIC,
    bootstrap_servers=service_url("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
    schema_registry_url=service_url("SCHEMA_REGISTRY_URL", "http://schema-registry:8081"),
    producer_name=SERVICE_NAME,
    logger=logger,
)
