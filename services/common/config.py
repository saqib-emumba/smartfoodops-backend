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

# Sibling service base URLs. Calls used to flow one way only — payment -> order -> menu ->
# restaurant -> user — with nothing addressing the Payment Service. The Week 2 saga closed
# that loop: the worker calls payments, restaurants and riders, and the Rider and Restaurant
# Services call back into orders to relay signals.
DEFAULT_USER_SERVICE_URL = "http://user-service:8001"
DEFAULT_RESTAURANT_SERVICE_URL = "http://restaurant-service:8002"
DEFAULT_MENU_SERVICE_URL = "http://menu-service:8003"
DEFAULT_ORDER_SERVICE_URL = "http://order-service:8004"
DEFAULT_PAYMENT_SERVICE_URL = "http://payment-service:8005"
DEFAULT_RIDER_SERVICE_URL = "http://rider-service:8006"

# The workflow orchestrator. gRPC, so no scheme.
DEFAULT_TEMPORAL_ADDRESS = "temporal-server:7233"

# One task queue for the order saga. Named here rather than in the workflow so the service
# that starts a workflow and the worker that runs it cannot disagree about where it goes.
ORDER_TASK_QUEUE = "order-tasks"

# Ceiling on a single inter-service HTTP round trip. Kept well below the client-facing
# timeout so a slow dependency surfaces as a 503 rather than hanging the caller.
HTTP_TIMEOUT = 5.0

# How long the mock card gateway pretends to take. A real authorisation is a round trip to
# an external processor and takes seconds, not milliseconds, and every timeout downstream of
# it should be sized for that rather than for a function call that returns instantly.
MOCK_GATEWAY_LATENCY_SECONDS = 5.0

# Calls to the Payment Service get their own, longer ceiling: HTTP_TIMEOUT is 5s, which the
# gateway latency above would race against and lose. Deliberately still *under* the payment
# activity's 20s start_to_close_timeout, so a genuinely hung gateway surfaces as a `503`
# from ServiceClient — a retryable error naming the dependency — rather than as a Temporal
# activity timeout, which says only that something took too long.
PAYMENT_HTTP_TIMEOUT = 15.0

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

# --- Order saga timings -----------------------------------------------------------------
#
# These are workflow-level waits, not HTTP timeouts, and the difference matters: a workflow
# waiting here holds no thread, no connection and no memory in any service. It is a durable
# timer in Temporal, so the numbers can be generous in a way an HTTP_TIMEOUT never can.

# How long a kitchen has to accept or decline before the order is cancelled and refunded.
# Generous on purpose: the alternative is cancelling orders a busy restaurant would have
# taken, which costs a real sale to save a few seconds.
RESTAURANT_DECISION_TIMEOUT_SECONDS = 120

# Rider search. Repeated short attempts separated by durable timers rather than one long
# call, because "no rider free right now" is a condition that resolves with time, and a
# single blocking attempt cannot wait for it. Total window is attempts x interval.
RIDER_SEARCH_ATTEMPTS = 6
RIDER_SEARCH_INTERVAL_SECONDS = 10

# Nothing beyond this is offered the order; a rider 30km away is not a delivery.
RIDER_MAX_DISTANCE_KM = 10.0

# The one leg with a human walking around in it, so its bound is an hour rather than
# seconds. Exceeding it means something went wrong that no retry will fix.
DELIVERY_TIMEOUT_SECONDS = 3600
