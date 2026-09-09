"""Process-lifetime singletons for the Rider Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from rider import deps` and reference `deps.riders`, not
`from rider.deps import riders`: the latter binds at import time.

`saga` is D47's addition: this service signals a pickup or delivery into the order's
workflow itself, rather than asking the Order Service to relay it. It still calls
`order_service` first, to record the stage against the order — that write is what the saga
reads back on a timeout, and an order's state is not this service's to hold (D01).
"""

from common.bootstrap import bootstrap
from common.config import DEFAULT_TEMPORAL_ADDRESS, service_url
from common.temporal import SagaClient, TemporalGateway
from rider.clients.order import OrderServiceClient
from rider.clients.user import UserServiceClient
from rider.repositories.riders import RiderRepository

TEMPORAL_ADDRESS = service_url("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)

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

temporal = TemporalGateway(TEMPORAL_ADDRESS, logger=logger)
saga = SagaClient(temporal, logger=logger)
