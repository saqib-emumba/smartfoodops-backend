"""Process-lifetime singletons for the Orchestrator Service's request-path API — starting a
saga and relaying a signal into one, the two things the Order Service calls over HTTP.

Here rather than in main.py because main.py imports the routers in order to include them,
so a router importing its collaborators from main.py would be a cycle.

No database: this is the one service in the platform with none (D36). Everything it needs
to answer a request is either in the call itself or in Temporal's own storage.

Routers should `from orchestrator import deps` and reference `deps.temporal`, not
`from orchestrator.deps import temporal`: the latter binds at import time and makes any
future swap impossible without touching every router.
"""

from common.bootstrap import bootstrap
from common.config import DEFAULT_TEMPORAL_ADDRESS, service_url
from common.temporal import TemporalGateway

TEMPORAL_ADDRESS = service_url("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)

runtime = bootstrap("orchestrator-service", db=False)

SERVICE_NAME = runtime.name
logger = runtime.logger

temporal = TemporalGateway(TEMPORAL_ADDRESS, logger=logger)
