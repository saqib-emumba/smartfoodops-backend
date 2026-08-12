"""Infrastructure defaults shared across services.

Every value here is a *local docker-compose* fallback for when the corresponding
environment variable is absent; docker-compose injects the real values. Production
deployments must supply DATABASE_URL and friends from a secret store rather than
relying on these checked-in development credentials.
"""

# Datastore endpoints (mirrored by the environment blocks in docker-compose.yml).
DEFAULT_DATABASE_URL = (
    "postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core"
)
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
