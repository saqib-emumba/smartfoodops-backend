"""Process-lifetime singletons for the Payment Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from payment import deps` and reference `deps.payments`, not
`from payment.deps import payments`: the latter binds at import time.
"""

from common.bootstrap import bootstrap
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
