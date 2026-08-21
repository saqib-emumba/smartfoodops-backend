"""Temporal client plumbing, shared by the Order Service and its worker.

Mirrors what `PostgresPool` does for Postgres: one place that knows how the connection is
made, exposes a FastAPI lifespan, and can answer whether the dependency is reachable
without raising. Two processes need a Temporal client — the Order Service, which starts
workflows and relays signals into them, and order-worker, which executes them — and this
is what keeps them from growing two different ways of connecting.

The Week 2 blueprint's first revision called `Client.connect()` inside the request handler,
paying for a TCP connect and a gRPC handshake on every single order.

A workflow id is derived from an order id rather than stored anywhere, which is the other
thing this module owns. That is what makes starting a saga idempotent: two attempts for one
order compute the same id, and Temporal is then the thing that refuses the duplicate.
"""

from contextlib import asynccontextmanager
from logging import Logger
from uuid import UUID

from temporalio.client import Client
from temporalio.service import RPCError

WORKFLOW_ID_PREFIX = "order-"


def workflow_id_for(order_id: UUID | str) -> str:
    """The deterministic workflow id for one order.

    Derived, never generated and never persisted. `create_order` uses it to start the
    saga, and the signal relay uses it to find the handle again — neither needs a column
    recording which workflow belongs to which order, because the id *is* the order id.
    """
    return f"{WORKFLOW_ID_PREFIX}{order_id}"


class TemporalGateway:
    """A lazily-connected Temporal client with a lifespan and a health probe."""

    def __init__(self, address: str, *, logger: Logger):
        self.address = address
        self._logger = logger
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        """The connected client.

        Raises rather than reconnecting on the spot: a handler reaching this with no client
        means the lifespan never ran, which is a wiring bug. Papering over it with a lazy
        connect would turn a startup failure into a slow, intermittent one.
        """
        if self._client is None:
            raise RuntimeError(
                "Temporal client is not initialised; TemporalGateway.lifespan did not run"
            )
        return self._client

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> Client:
        self._client = await Client.connect(self.address)
        self._logger.info("Connected to Temporal at %s", self.address)
        return self._client

    @asynccontextmanager
    async def lifespan(self, _=None):
        """FastAPI lifespan; composed with `PostgresPool.lifespan` via `AsyncExitStack`.

        A failure to reach Temporal at startup is logged and swallowed rather than fatal.
        Orders must stay creatable and readable while the orchestrator is down — the
        database is what the customer's order actually lives in, and refusing to boot
        would take reads down too. What fails instead is the workflow start, and that is
        repaired by an idempotent retry.
        """
        try:
            await self.connect()
        except Exception as exc:  # noqa: BLE001 - startup must survive a missing orchestrator
            self._logger.error(
                "Temporal unreachable at %s; sagas will not start until it returns: %s",
                self.address,
                exc,
            )
        yield

    async def is_reachable(self) -> bool:
        """Whether the orchestrator answers. Never raises — this feeds a health endpoint."""
        if self._client is None:
            return False
        try:
            await self._client.service_client.check_health()
            return True
        except (RPCError, OSError) as exc:
            self._logger.warning("Temporal health check failed: %s", exc)
            return False
