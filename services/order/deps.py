"""Process-lifetime singletons for the Order Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from order import deps` and reference `deps.orders`, not
`from order.deps import orders`: the latter binds at import time and makes any future swap
impossible without touching every router.

No Temporal client any more. Before D36 this held a `TemporalGateway` directly, alongside
`order-worker`, the only other process in the platform that did; now starting and signalling
a saga are HTTP calls into the Orchestrator Service, through `orchestrator_service` below,
and this service holds no orchestration state of its own.
"""

from common.bootstrap import bootstrap
from order.clients.menu import MenuServiceClient
from order.clients.orchestrator import OrchestratorClient
from order.clients.restaurant import RestaurantServiceClient
from order.clients.user import UserServiceClient
from order.repositories.orders import OrderRepository
from order.repositories.tracking import OrderTrackingRepository

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
orchestrator_service = OrchestratorClient(logger)
