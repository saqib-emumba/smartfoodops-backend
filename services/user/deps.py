"""Process-lifetime singletons for the User Service.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

Routers should `from user import deps` and reference `deps.users`, not
`from user.deps import users`: the latter binds at import time.
"""

from common.bootstrap import bootstrap
from common.config import DEFAULT_AUTH_REDIS_URL, service_url
from user.repositories.sessions import RefreshTokenStore
from user.repositories.users import UserRepository

AUTH_REDIS_URL = service_url("AUTH_REDIS_URL", DEFAULT_AUTH_REDIS_URL)

runtime = bootstrap("user-service")

SERVICE_NAME = runtime.name
logger = runtime.logger
db = runtime.db

users = UserRepository(db)
refresh_tokens = RefreshTokenStore(AUTH_REDIS_URL, logger=logger)
