"""Infrastructure configuration shared across services.

Credentials are never defaulted in code. Anything carrying a secret — DATABASE_URL, the
JWT signing keys and the internal API key — must arrive from the environment, which
docker-compose builds from the gitignored root `.env`. A missing value fails the service at
startup rather than silently falling back to a checked-in password.

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
DEFAULT_REDIS_URL = "redis://cache-redis:6379/0"

# Refresh tokens live in logical database 1, keeping them clear of the Menu Service's
# cache in database 0: an accidental FLUSHDB on one must not sign every user out.
DEFAULT_AUTH_REDIS_URL = "redis://cache-redis:6379/1"

# Token lifetimes. The access token is deliberately short because it cannot be revoked
# before it expires — logout invalidates the refresh token, not tokens already issued.
ACCESS_TOKEN_TTL_MINUTES = 15
REFRESH_TOKEN_TTL_DAYS = 7

# Sibling service base URLs. Calls flow payment -> order -> menu -> restaurant -> user, so
# every service except the Payment Service is addressed by something: nothing calls into
# payments yet — the Temporal workflow that will is Week 2 work.
DEFAULT_USER_SERVICE_URL = "http://user-service:8001"
DEFAULT_RESTAURANT_SERVICE_URL = "http://restaurant-service:8002"
DEFAULT_MENU_SERVICE_URL = "http://menu-service:8003"
DEFAULT_ORDER_SERVICE_URL = "http://order-service:8004"

# Ceiling on a single inter-service HTTP round trip. Kept well below the client-facing
# timeout so a slow dependency surfaces as a 503 rather than hanging the caller.
HTTP_TIMEOUT = 5.0

# Ceiling on a Redis round trip. Well under HTTP_TIMEOUT: the cache sits inside a request
# that has its own deadline, and a slow cache must fall back to Postgres rather than spend
# the whole budget waiting for the copy.
REDIS_TIMEOUT = 2.0

# How long a cached menu may outlive the row it was read from. Only reached when an
# invalidation is lost (see services/menu/cache.py), so it is a backstop, not the plan.
MENU_CACHE_TTL_SECONDS = 3600

# Bounds on each service's PostgreSQL connection pool.
POOL_MIN_CONNECTIONS = 1
POOL_MAX_CONNECTIONS = 10
