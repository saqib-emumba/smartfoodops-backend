"""Process-lifetime singletons for the Order Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from order import deps` and reference `deps.orders`, not
`from order.deps import orders`: the latter binds at import time and makes any future swap
impossible without touching every router.

Never imported by workflows.py, activities.py or worker.py: that boundary is what keeps the
worker from opening a second connection pool or resolving URLs it was never given — see
clients/__init__.py.
"""

from common.bootstrap import bootstrap
from common.config import DEFAULT_TEMPORAL_ADDRESS, service_url
from common.temporal import TemporalGateway
from order.clients.menu import MenuServiceClient
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
