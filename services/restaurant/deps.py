"""Process-lifetime singletons for the Restaurant Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle. This module imports
neither, which is the whole of why it exists — it is main.py's wiring block, moved.

Routers should `from restaurant import deps` and reference `deps.restaurants`, not
`from restaurant.deps import restaurants`: the latter binds at import time and makes any
future swap a change to every router.
"""

from common.bootstrap import bootstrap
from restaurant.clients.user import UserServiceClient
from restaurant.repositories.restaurants import RestaurantRepository

runtime = bootstrap("restaurant-service")

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

restaurants = RestaurantRepository(db)
user_service = UserServiceClient(logger)
