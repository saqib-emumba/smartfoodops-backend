"""Process-lifetime singletons for the Menu Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from menu import deps` and reference `deps.menus`, not
`from menu.deps import menus`: the latter binds at import time.
"""

from common.bootstrap import bootstrap
from common.config import DEFAULT_REDIS_URL, service_url
from menu.clients.restaurant import RestaurantServiceClient
from menu.repositories.cache import MenuCache
from menu.repositories.menus import MenuRepository

REDIS_URL = service_url("REDIS_URL", DEFAULT_REDIS_URL)

runtime = bootstrap(
    "menu-service",
    exhausted_detail="Database connection pool exhausted; the menu could not be served",
)

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

menus = MenuRepository(db)
cache = MenuCache(REDIS_URL, logger=logger)
restaurant_service = RestaurantServiceClient(logger)
