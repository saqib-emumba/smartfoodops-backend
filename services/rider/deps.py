"""Process-lifetime singletons for the Rider Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from rider import deps` and reference `deps.riders`, not
`from rider.deps import riders`: the latter binds at import time.
"""

from common.bootstrap import bootstrap
from rider.clients.order import OrderServiceClient
from rider.clients.user import UserServiceClient
from rider.repositories.riders import RiderRepository

runtime = bootstrap(
    "rider-service",
    exhausted_detail="Database connection pool exhausted; no rider was dispatched",
)

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

riders = RiderRepository(db, logger=logger)
user_service = UserServiceClient(logger)
order_service = OrderServiceClient(logger)
