"""The process that executes order workflows and their activities.

Its own deployable now, sharing nothing with the Order Service — not the image, not the
database (D36). The workflow and its activities are Temporal's code, orchestrating facts
the other services own; they were never really the Order Service's own data, only
co-located with it because Week 2 built the saga inside the service whose lifecycle it
drives. The first revision of that blueprint supplied a worker file but never scheduled it,
so nothing would have run the saga at all — this file's job hasn't changed since.

`activity_executor` is still load-bearing, for a different reason than before. Every
activity here is now an HTTP call rather than a mix of HTTP and sync psycopg2, but `httpx`'s
synchronous client still blocks, and Temporal's Python SDK runs sync activities on a thread
pool for exactly that reason (D21 is why they stayed sync rather than growing an async
client alongside the sync one). Sized generically now, not against a connection pool this
process no longer has — see `orders`/`activities` construction below, which takes an
`OrderServiceClient` instead of the `OrderRepository` this file built before the split.

One worker process, one `Worker` per task queue, and — since `activities/`, `clients/` and
`workflows/` are all entity-scoped — potentially several entities registered on it. What
runs is declared in `registry.py`, not here: this file knows how to *run* a registration,
not which ones exist. A second entity needs `workflows/<entity>.py`,
`activities/<entity>.py`, a task-queue constant, and one line in that registry — and no
HTTP surface at all, since services name workflows by string through
`common.temporal.SagaClient`. A second *container* is only warranted if a future entity
should scale or deploy independently of this one.

Connects through `TemporalGateway` rather than a bare `Client.connect()` (Week 3): that is
the one place `TracingInterceptor` is wired in, so this process and the Orchestrator
Service's API side connect exactly the same way. Per Temporal's own guidance, the
interceptor is registered on the client only — `Worker(...)` inherits it from the client
it is built with, and registering it a second time on the Worker itself would duplicate
every span.

Also starts a bare `prometheus_client` HTTP server on `:9108`: this process is not a FastAPI
app, so it has no `/metrics` route of its own to reuse the app-scoped wiring in
`common/telemetry.py` — this is its only scrape target.
"""

import asyncio
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from datetime import timedelta

from prometheus_client import start_http_server
from temporalio.worker import Worker

from common.bootstrap import bootstrap
from common.config import DEFAULT_TEMPORAL_ADDRESS
from common.temporal import TemporalGateway
from orchestrator.registry import registrations

SERVICE_NAME = "orchestrator-worker"

# This process has no FastAPI app and therefore no /metrics route of its own — a fixed
# port, scraped directly, is its only telemetry surface for Prometheus.
METRICS_PORT = 9108


async def main() -> None:
    address = os.getenv("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)

    # No database: this process holds no state of its own, so bootstrap builds a logger
    # and nothing else. `required("DATABASE_URL")` is never even checked. bootstrap() also
    # calls configure_telemetry(), so the TracerProvider TracingInterceptor attaches to
    # below is already installed by the time TemporalGateway.connect() runs.
    runtime = bootstrap(SERVICE_NAME, db=False)
    logger = runtime.logger

    start_http_server(METRICS_PORT)
    logger.info("Prometheus metrics server listening on :%d", METRICS_PORT)

    temporal = TemporalGateway(address, logger=logger)
    client = await temporal.connect()
    registered = registrations(logger)

    # No connection pool to bound against any more; a modest fixed size instead of the
    # unbounded default, since each activity call is a blocking HTTP request and the point
    # of an executor at all is to stop one slow one from stalling every other workflow.
    # Shared across every Worker below: the bound that matters is this process's total
    # concurrent activity count, not a per-task-queue one.
    with ThreadPoolExecutor(max_workers=20, thread_name_prefix="sfo-activity") as executor:
        # `docker compose stop` sends SIGTERM. Without a handler nothing listens, the
        # container is SIGKILLed after the grace period, and in-flight activities are
        # abandoned rather than finishing and reporting back.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        # One Worker per task queue — a Worker polls exactly one — entered together and
        # exited in reverse, the same AsyncExitStack shape `common/lifespan.py` uses to
        # compose a service's dependencies. `Worker.run()` blocks forever and takes no
        # arguments; entering it as an async context manager is how the SDK exposes
        # "run until I say stop".
        async with AsyncExitStack() as stack:
            for registration in registered:
                await stack.enter_async_context(
                    Worker(
                        client,
                        task_queue=registration.task_queue,
                        workflows=list(registration.workflows),
                        activities=list(registration.activities),
                        activity_executor=executor,
                        # Time allowed for in-flight activities to finish after shutdown
                        # starts. Anything still running when it expires is cancelled —
                        # and because every activity is idempotent, Temporal simply
                        # re-runs it on the next worker, which is the property the
                        # durability test exercises.
                        graceful_shutdown_timeout=timedelta(seconds=30),
                    )
                )
                # Logged per queue, and the resilience suite greps for this exact prefix
                # to know the worker is back after a restart — see
                # scripts/saga-resilience-test.sh's "Worker polling" poll.
                logger.info(
                    "Worker polling task queue '%s' at %s",
                    registration.task_queue,
                    address,
                )

            await stop.wait()
        logger.info("Worker shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
