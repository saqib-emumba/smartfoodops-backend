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

One worker process, one task queue, and — since `activities/`, `clients/`, `schemas/` and
`workflows/` were all made entity-scoped — potentially several entities' workflows and
activities registered on it. A second entity does not need a second `worker.py`: it needs
its own `OrderWorkflow`-shaped class in `workflows/<entity>.py` and `Activities` class in
`activities/<entity>.py`, both added to the `workflows=[...]`/`activities=[...]` lists
below, exactly like `OrderWorkflow` and `OrderActivities` are today. A second task queue,
and therefore a second `Worker(...)`, is only needed if a future entity's workflows should
scale or deploy independently of this one.

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
from datetime import timedelta

from prometheus_client import start_http_server
from temporalio.worker import Worker

from common.bootstrap import bootstrap
from common.config import DEFAULT_TEMPORAL_ADDRESS, ORDER_TASK_QUEUE
from common.temporal import TemporalGateway
from orchestrator.activities.order import OrderActivities
from orchestrator.clients.order.order_service import OrderServiceClient
from orchestrator.workflows.order import OrderWorkflow

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
    activities = OrderActivities(orders=OrderServiceClient(logger), logger=logger)

    # No connection pool to bound against any more; a modest fixed size instead of the
    # unbounded default, since each activity call is a blocking HTTP request and the point
    # of an executor at all is to stop one slow one from stalling every other workflow.
    with ThreadPoolExecutor(max_workers=20, thread_name_prefix="sfo-activity") as executor:
        worker = Worker(
            client,
            task_queue=ORDER_TASK_QUEUE,
            workflows=[OrderWorkflow],
            activities=[
                activities.transition_order_activity,
                activities.authorize_payment_activity,
                activities.refund_payment_activity,
                activities.read_kitchen_decision_activity,
                activities.dispatch_rider_activity,
                activities.release_rider_activity,
                # Week 3, D46 — a seventh activity added to a live registration list. Safe
                # for workflows *started* after this deploys; see workflows/order.py's own
                # comment on why a workflow already mid-flight needs draining first, not
                # this addition alone, to replay safely against the new code.
                activities.read_rider_report_activity,
            ],
            activity_executor=executor,
            # Time allowed for in-flight activities to finish after shutdown starts.
            # Anything still running when it expires is cancelled — and because every
            # activity here is idempotent, Temporal simply re-runs it on the next
            # worker, which is the property the durability test exercises.
            graceful_shutdown_timeout=timedelta(seconds=30),
        )

        # `docker compose stop` sends SIGTERM. Without a handler nothing listens, the
        # container is SIGKILLed after the grace period, and in-flight activities are
        # abandoned rather than finishing and reporting back.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        logger.info("Worker polling task queue '%s' at %s", ORDER_TASK_QUEUE, address)
        # `Worker.run()` takes no arguments and blocks forever; entering the worker as
        # an async context manager is how the SDK exposes "run until I say stop".
        async with worker:
            await stop.wait()
        logger.info("Worker shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
