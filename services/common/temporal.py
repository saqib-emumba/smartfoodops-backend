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

import uuid
from contextlib import asynccontextmanager
from logging import Logger
from uuid import UUID

from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowFailureError,
    WorkflowUpdateFailedError,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from common.errors import (
    bad_gateway,
    bad_request,
    conflict,
    forbidden,
    internal_error,
    not_found,
    service_unavailable,
    unauthorized,
    unprocessable,
)

# D53/D54: an activity that fails non-retryably tags its `ApplicationError` with one of these
# `type=` strings (see orchestrator/activities/order.py and activities/payment.py) so the
# client-side unwrap below can recover the actual business status instead of the generic
# "Activity task failed" every wrapping layer (ActivityError, then
# WorkflowUpdateFailedError/WorkflowFailureError) would otherwise report if just stringified.
_ERROR_TYPE_STATUS = {
    "OrderCreateRejected": unprocessable,
    "OrderCreateConflict": conflict,
    "ManualPaymentRejected": unprocessable,
    "ManualPaymentConflict": conflict,
}

# The generic fallback: an activity that catches a plain `HTTPException` it has no
# more specific type for tags it `http_<status>` (see e.g.
# orchestrator/activities/order.py::create_order_activity's final `except HTTPException`)
# rather than dropping the status code on the floor. Covers cases a `passthrough` dict
# wasn't written for — a 403 from an ownership check three services away from the one
# that started this Update, a 404 for "no menu published yet" — without needing a new named
# exception type for every status a downstream call could plausibly answer with.
_HTTP_STATUS_FACTORIES = {
    400: bad_request,
    401: unauthorized,
    403: forbidden,
    404: not_found,
    409: conflict,
    422: unprocessable,
    500: internal_error,
    502: bad_gateway,
    503: service_unavailable,
}


def _status_factory(error_type: str | None):
    if error_type in _ERROR_TYPE_STATUS:
        return _ERROR_TYPE_STATUS[error_type]
    if error_type and error_type.startswith("http_"):
        try:
            return _HTTP_STATUS_FACTORIES.get(int(error_type[len("http_") :]))
        except ValueError:
            return None
    return None


def _find_application_error(exc: BaseException) -> ApplicationError | None:
    """Walk an exception's `.cause` chain for the `ApplicationError` an activity actually
    raised. Temporal wraps it in at least one layer (an `ActivityError` inside the workflow,
    then a `WorkflowUpdateFailedError`/`WorkflowFailureError` on the client) by the time it
    reaches here, and `str()` on those wrapper types says only "Activity task failed"."""
    seen: BaseException | None = exc
    for _ in range(6):
        if isinstance(seen, ApplicationError):
            return seen
        seen = getattr(seen, "cause", None)
        if seen is None:
            return None
    return None


def _raise_mapped(exc: BaseException, *, default) -> None:
    """Re-raise `exc` as the HTTP exception its underlying `ApplicationError.type` maps to,
    falling back to `default(str(exc))` when there's no recognised type — an unexpected
    failure shape, still worth reporting as *something* rather than a raw 500."""
    found = _find_application_error(exc)
    if found is not None:
        factory = _status_factory(found.type) or default
        raise factory(found.message or str(found)) from exc
    raise default(str(exc)) from exc

WORKFLOW_ID_PREFIX = "order-"

# D53: the order-creation Update handler derives `order_id` from `(customer_id,
# idempotency_key)` before Postgres or Temporal has ever heard of the order — see
# readme/order-creation-temporal-update-design.md ss3. A fixed namespace UUID (arbitrary,
# generated once) is what makes uuid5 deterministic across processes and restarts; changing
# it would silently re-derive every future order id.
ORDER_ID_NAMESPACE = uuid.UUID("5b1f5a3a-6b7b-4b3e-8a7a-3a2b6b6e2f1a")


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


# D55 (mentor's child-workflow split): OrderWorkflow's payment, rider and compensation
# steps each moved into their own workflow type, started as a child of the order saga. Each
# gets its own deterministic id, addressed the same way `workflow_id_for` already addresses
# OrderWorkflow itself — one entity, one prefix, one function, so the service that starts a
# child and (for `rider`) the sibling that signals it later can never disagree about its
# name.
def payment_workflow_id_for(order_id: UUID | str) -> str:
    """`PaymentWorkflow`'s id for the order saga's own authorisation step."""
    return workflow_id("payment", order_id)


def manual_payment_workflow_id_for(order_id: UUID | str) -> str:
    """`PaymentWorkflow`'s id for the direct/manual endpoint (`process_payment`, D30) —
    deliberately a *different* id than `payment_workflow_id_for` addresses, even though both
    start the same workflow type.

    Tried sharing one id across both paths first: once the saga's `PaymentWorkflow`
    execution under `payment-<id>` had completed, a later manual attempt against that same
    id came back as that *original* execution's result — the saga's authorised amount, not
    whatever the manual request actually asked to charge — rather than genuinely starting
    a fresh run. Whatever policy would make "start under a completed id" behave like a true
    restart is not the default here, so two entity-distinct ids sidestep the question
    entirely: each path only ever addresses an execution it started itself. D30's real
    protection against double-paying one order was always the database's own
    `UNIQUE(order_id)` constraint, never the workflow id — that still holds unchanged."""
    return workflow_id("payment-manual", order_id)


def rider_workflow_id_for(order_id: UUID | str) -> str:
    """`RiderWorkflow`'s id. The Rider Service signals pickup/delivery directly into this
    workflow now — see `services/rider/fleet.py::report_event` — not into `OrderWorkflow`,
    which no longer holds `rider_pickup`/`rider_delivery` signal handlers at all."""
    return workflow_id("rider", order_id)


def compensation_workflow_id_for(order_id: UUID | str) -> str:
    """`CompensationWorkflow`'s id — started at most once per order, by `OrderWorkflow`
    itself, whenever the saga cannot proceed."""
    return workflow_id("compensation", order_id)


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

    async def run(self, workflow: str, *, task_queue: str, wf_id: str, payload: dict) -> dict:
        """Start a workflow and wait for it to finish, returning its result.

        For a short, self-contained workflow with no saga to keep running after the request
        returns (`PaymentWorkflow`'s manual mode, D53/D55) — unlike `start`, which only
        hands back a workflow id because the order saga is meant to outlive the request
        that started it.
        """
        if not self._gateway.connected:
            raise service_unavailable("Temporal is unreachable; the payment was not processed")

        try:
            return await self._gateway.client.execute_workflow(
                workflow,
                payload,
                id=wf_id,
                task_queue=task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        except WorkflowFailureError as exc:
            _raise_mapped(exc, default=conflict)

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

    async def update(self, wf_id: str, update: str, payload: dict) -> dict:
        """Send a Temporal Update into an already-running workflow and wait for its result.

        Unlike `signal`, this is not best-effort: an Update is a synchronous RPC with a
        return value, so a caller that needs to know the outcome (D53's kitchen-decision
        Update, replacing a write-then-best-effort-signal) uses this instead of `signal`.
        """
        if not self._gateway.connected:
            raise service_unavailable(f"Temporal is unreachable; '{update}' was not recorded")

        handle = self._gateway.client.get_workflow_handle(wf_id)
        try:
            return await handle.execute_update(update, payload)
        except WorkflowUpdateFailedError as exc:
            _raise_mapped(exc, default=conflict)
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                raise not_found(
                    f"Saga {wf_id} is not running; it may have already finished or been "
                    "cancelled"
                ) from exc
            self._logger.error("Could not send update '%s' to saga %s: %s", update, wf_id, exc)
            raise conflict(f"The saga {wf_id} would not accept update '{update}'") from exc

    async def start_with_update(
        self,
        workflow: str,
        *,
        task_queue: str,
        wf_id: str,
        update: str,
        update_payload: dict,
    ) -> dict:
        """Update-with-Start (D53/the order-creation design): atomically start a workflow if
        it is not already running, deliver an Update to it, and block for the Update's
        result — one round trip for "the order exists and the saga is running", instead of
        an insert followed by a separate, best-effort saga start (D25).

        `USE_EXISTING` is what makes a retried request safe: it routes the Update to the
        workflow already running under this id rather than starting a duplicate, exactly as
        `start()` already relies on for a retried checkout.
        """
        if not self._gateway.connected:
            raise service_unavailable("Temporal is unreachable; the order was not created")

        start_operation = WithStartWorkflowOperation(
            workflow,
            args=[],
            id=wf_id,
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        try:
            return await self._gateway.client.execute_update_with_start_workflow(
                update,
                args=[update_payload],
                start_workflow_operation=start_operation,
            )
        except WorkflowUpdateFailedError as exc:
            _raise_mapped(exc, default=conflict)
