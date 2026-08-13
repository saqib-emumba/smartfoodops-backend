"""Infrastructure configuration shared across services.

Credentials are never defaulted in code. Anything carrying a secret — currently just
DATABASE_URL — must arrive from the environment, which docker-compose builds from the
gitignored root `.env`. A missing value fails the service at startup rather than silently
falling back to a checked-in password.

The defaults that remain here are credential-free in-network addresses, so publishing them
in source costs nothing and keeps local runs friction-free.
"""

import os


def required(name: str) -> str:
    """Read a mandatory environment variable, failing loudly at startup if unset.

    docker-compose supplies these from the root `.env`; export them by hand when running
    a service directly on the host.
    """
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. docker-compose.yml supplies it from the root .env file; "
            f"export it manually when running this service outside Docker."
        )
    return value


# Credential-free datastore endpoints (mirrored by docker-compose.yml).
DEFAULT_MONGO_URI = "mongodb://db-nosql:27017/smartfoodops_menus"
DEFAULT_REDIS_URL = "redis://cache-redis:6379/0"

# Sibling service base URLs.
DEFAULT_USER_SERVICE_URL = "http://user-service:8001"
DEFAULT_RESTAURANT_SERVICE_URL = "http://restaurant-service:8002"
DEFAULT_MENU_SERVICE_URL = "http://menu-service:8003"

# Ceiling on a single inter-service HTTP round trip. Kept well below the client-facing
# timeout so a slow dependency surfaces as a 503 rather than hanging the caller.
HTTP_TIMEOUT = 5.0

# Ceiling on MongoDB server-selection and socket operations.
MONGO_TIMEOUT_MS = 5000

# Bounds on each service's PostgreSQL connection pool.
POOL_MIN_CONNECTIONS = 1
POOL_MAX_CONNECTIONS = 10
