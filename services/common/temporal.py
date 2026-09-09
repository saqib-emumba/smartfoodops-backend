"""Temporal client plumbing, shared by every process that talks to Temporal.

Mirrors what `PostgresPool` does for Postgres: one place that knows how the connection is
made, exposes a FastAPI lifespan, and can answer whether the dependency is reachable
without raising. Four processes need a client now — the Order Service, which starts the
saga and signals the kitchen's decision into it; the Rider Service, which signals pickups
and deliveries; orchestrator-service, for its health probe; and orchestrator-worker, which
executes the workflows — and this is what keeps them from growing four ways of connecting.

The Week 2 blueprint's first revision called `Client.connect()` inside the request handler,
paying for a TCP connect and a gRPC handshake on every single order.

A workflow id is derived from a business id rather than stored anywhere, which is the other
thing this module owns. That is what makes starting a saga idempotent: two attempts for one
order compute the same id, and Temporal is then the thing that refuses the duplicate.
"""

from contextlib import asynccontextmanager
from logging import Logger
from uuid import UUID

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.service import RPCError, RPCStatusCode

from common.errors import conflict, not_found, service_unavailable

WORKFLOW_ID_PREFIX = "order-"


def workflow_id(entity: str, entity_id: UUID | str) -> str:
    """The deterministic workflow id for one instance of one orchestrated entity.

    Generic because the orchestrator is meant to grow a second workflow: a new entity gets
    its own prefix here rather than its own id-building function somewhere else, so the
    service that starts a workflow and the worker that runs it cannot disagree about what
    it is called.
    """
    return f"{entity}-{entity_id}"


def workflow_id_for(order_id: UUID | str) -> str:
    """The deterministic workflow id for one order.

    Derived, never generated and never persisted. `create_order` uses it to start the
    saga, and each signalling service uses it to find the handle again — neither needs a
    column recording which workflow belongs to which order, because the id *is* the order
    id.

    Delegates to `workflow_id` rather than formatting its own string, and must keep
    producing exactly `order-<uuid>`: this is how a *running* saga is found, so a change
    here orphans every workflow already in flight.
    """
    return workflow_id(WORKFLOW_ID_PREFIX.rstrip("-"), order_id)


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
        # TracingInterceptor picks up whatever TracerProvider configure_telemetry already
        # installed globally — it does not need its own Resource or exporter. Without it, a
        # trace started by an HTTP handler ends at the client.start_workflow() call: nothing
        # here would carry the trace context into the workflow, and durable-history spans
        # (activities, signals, timers) would appear as an orphaned, service-less trace.
        self._client = await Client.connect(
            self.address, interceptors=[TracingInterceptor()]
        )
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


class SagaClient:
    """Start and signal workflows by name, for the services that observe saga facts.

    Every name this takes is a **string** — the workflow type, the signal, the query — and
    that is the whole point rather than a convenience. Passing `OrderWorkflow.run` instead
    would make the calling service import `orchestrator.workflows.order`, which imports
    `activities/order.py`, which loads the payment and rider clients into a process that
    has neither `PAYMENT_SERVICE_URL` nor `RIDER_SERVICE_URL` set. That is the exact fault
    D36 removed by putting an HTTP hop in front of Temporal; naming the workflow by string
    removes it without the hop, and keeps the hop from being needed again.

    So no service outside `services/orchestrator/` may import a workflow or activity
    module. The contract between them is this class plus the task-queue and signal-name
    constants in `common/config.py` — which is also what makes a second workflow cheap:
    it needs no new API surface, only a new name.

    Errors are translated once, here, rather than in each caller: `NOT_FOUND` from Temporal
    means "no such workflow, or it already finished", and the two are genuinely
    indistinguishable to a caller — the same ambiguity `orchestrator/apis/order.py` used to
    resolve on everyone's behalf.
    """

    def __init__(self, gateway: TemporalGateway, *, logger: Logger):
        self._gateway = gateway
        self._logger = logger

    @property
    def connected(self) -> bool:
        return self._gateway.connected

    async def start(
        self, workflow: str, *, task_queue: str, wf_id: str, payload: dict
    ) -> str:
        """Start a workflow, or hand back the one already running under this id.

        `USE_EXISTING` is what makes this safe to call more than once: the id is derived
        from a business key, so a retried checkout names the same workflow and Temporal
        returns the running one rather than raising. There is deliberately no
        `except WorkflowAlreadyStartedError` for the same reason.
        """
        if not self._gateway.connected:
            raise service_unavailable("Temporal is unreachable; the saga was not started")

        handle = await self._gateway.client.start_workflow(
            workflow,
            payload,
            id=wf_id,
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        self._logger.info("Saga %s running (%s)", handle.id, workflow)
        return handle.id

    async def signal(self, wf_id: str, signal: str, payload: dict | None = None) -> None:
        """Relay one event into a running workflow.

        No handle is stored anywhere — the workflow id is a pure function of the business
        id, so finding the running saga costs nothing to remember.
        """
        if not self._gateway.connected:
            raise service_unavailable("Temporal is unreachable; the saga was not signalled")

        handle = self._gateway.client.get_workflow_handle(wf_id)
        try:
            await handle.signal(signal, payload or {})
        except RPCError as exc:
            # NOT_FOUND covers both "no such workflow" and "it already finished" — the
            # caller has no way to tell them apart either, so it is reported as one thing.
            if exc.status is RPCStatusCode.NOT_FOUND:
                raise not_found(
                    f"Saga {wf_id} is not running; it may have already finished or been "
                    "cancelled"
                ) from exc
            self._logger.error("Could not signal saga %s: %s", wf_id, exc)
            raise conflict(f"The saga {wf_id} would not accept signal '{signal}'") from exc

        self._logger.info("Signalled '%s' to saga %s", signal, wf_id)

    async def query(self, wf_id: str, query: str):
        """Read a running workflow's own view of itself, without touching a database."""
        if not self._gateway.connected:
            raise service_unavailable("Temporal is unreachable; the saga cannot be queried")

        handle = self._gateway.client.get_workflow_handle(wf_id)
        try:
            return await handle.query(query)
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                raise not_found(f"Saga {wf_id} is not running") from exc
            raise conflict(f"The saga {wf_id} would not answer query '{query}'") from exc
