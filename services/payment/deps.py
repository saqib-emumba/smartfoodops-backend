"""Process-lifetime singletons for the Payment Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from payment import deps` and reference `deps.payments`, not
`from payment.deps import payments`: the latter binds at import time.

`kafka` replaces Week 3's `outbox_relay` (D39 -> D53): a lazily-connected producer
`apis/internal_events.py` calls synchronously, on demand, instead of a background relay
polling `payment_outbox` — same shared Kafka topic order-service publishes to either way
(D40), so payment and order events for one order still land in the same partition.

`temporal`/`payment_saga` are new as of D53: the direct/manual payment endpoint
(`apis/payments.py::process_payment`) now runs behind `PaymentWorkflow` (manual mode, D55)
rather than writing directly, so this service needs a Temporal client for the first time.
"""

from common.bootstrap import bootstrap
from common.config import DEFAULT_TEMPORAL_ADDRESS, service_url
from common.kafka import KafkaGateway
from common.temporal import SagaClient, TemporalGateway
from payment.clients.order import OrderServiceClient
from payment.clients.orchestrator import PaymentOrchestratorClient
from payment.gateway import MockPaymentGateway
from payment.repositories.payments import PaymentRepository

TEMPORAL_ADDRESS = service_url("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)

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

temporal = TemporalGateway(TEMPORAL_ADDRESS, logger=logger)
payment_saga = PaymentOrchestratorClient(SagaClient(temporal, logger=logger), logger=logger)

kafka = KafkaGateway(
    bootstrap_servers=service_url("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092"),
    schema_registry_url=service_url("SCHEMA_REGISTRY_URL", "http://schema-registry:8081"),
    producer_name=SERVICE_NAME,
    logger=logger,
)
