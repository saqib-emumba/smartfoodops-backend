"""The process that executes order workflows and their activities.

Runs in its own container, sharing the Order Service's image because the workflow and its
activities are that service's code — the order lifecycle is its fact. Only the command
differs. The first revision of the Week 2 blueprint supplied a worker file but never
scheduled it, so nothing would have run the saga at all.

`activity_executor` is the load-bearing argument here. The activities are sync psycopg2
functions, and without a thread pool they would block the worker's event loop: one slow
database call would stall every other workflow this worker is running. Handing Temporal an
executor is what lets the platform keep one way of reaching Postgres (D21) instead of
introducing an async engine solely for the saga.
"""

import asyncio
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from activities import OrderActivities
from common.config import (
    DEFAULT_TEMPORAL_ADDRESS,
    ORDER_TASK_QUEUE,
    POOL_MAX_CONNECTIONS,
    required,
)
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from repository import OrderRepository
from workflows import OrderWorkflow

SERVICE_NAME = "order-worker"


async def main() -> None:
    logger = configure_logging(SERVICE_NAME)
    address = os.getenv("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)

    db = PostgresPool(
        required("DATABASE_URL"),
        logger=logger,
        exhausted_detail="Database connection pool exhausted; saga step deferred",
    )

    # PostgresPool.lifespan is an async context manager written for FastAPI, and reusing it
    # here rather than opening the pool by hand is the point: the worker gets the same
    # connection handling, and the same shutdown, as every request handler.
    async with db.lifespan(None):
        client = await Client.connect(address)
        activities = OrderActivities(
            orders=OrderRepository(db, logger=logger, service_name=SERVICE_NAME),
            logger=logger,
        )

        # Never more threads than the pool has connections. A thread that cannot lease one
        # turns a transient shortage into a failed activity, so the executor is sized to
        # the real constraint rather than to the CPU.
        with ThreadPoolExecutor(
            max_workers=POOL_MAX_CONNECTIONS, thread_name_prefix="sfo-activity"
        ) as executor:
            worker = Worker(
                client,
                task_queue=ORDER_TASK_QUEUE,
                workflows=[OrderWorkflow],
                activities=[
                    activities.transition_order_activity,
                    activities.authorize_payment_activity,
                    activities.refund_payment_activity,
                    activities.send_ticket_activity,
                    activities.expire_ticket_activity,
                    activities.dispatch_rider_activity,
                    activities.release_rider_activity,
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

            logger.info(
                "Worker polling task queue '%s' at %s", ORDER_TASK_QUEUE, address
            )
            # `Worker.run()` takes no arguments and blocks forever; entering the worker as
            # an async context manager is how the SDK exposes "run until I say stop".
            async with worker:
                await stop.wait()
            logger.info("Worker shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
