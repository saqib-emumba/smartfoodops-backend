"""SmartFoodOps Order Service — idempotent checkout (Port 8004).

Owns the `orders` table in its own PostgreSQL database, and the `order_tracking_logs`
trail beside it — payments moved out to the Payment Service (Port 8005) along with their
table, while the trail moved in from the Menu Service's MongoDB, because which state an
order is in is this service's fact. Prices are always recalculated server-side from the
Menu Service's published menu.

The customer and restaurant an order names live in other services' databases, so they are
verified over HTTP before the insert — see clients.py.
"""

import os
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Response, status
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from clients import MenuServiceClient, RestaurantServiceClient, UserServiceClient
from common.auth import (
    Principal,
    current_principal,
    require_internal,
    require_role,
    require_self_or_admin,
)
from common.config import DEFAULT_TEMPORAL_ADDRESS, ORDER_TASK_QUEUE, required
from common.errors import bad_request, conflict, not_found
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from common.temporal import TemporalGateway, workflow_id_for
from pricing import build_order_snapshot
from repository import OrderRepository, OrderTrackingRepository
from schemas import (
    KitchenDecisionResponse,
    KitchenOrderResponse,
    OrderCreateRequest,
    OrderResponse,
    OrderTrackingLogCreateRequest,
    OrderTrackingLogResponse,
    WorkflowSignalRequest,
)
from workflows import OrderWorkflow

SERVICE_NAME = "order-service"
DATABASE_URL = required("DATABASE_URL")
TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(
    DATABASE_URL,
    logger=logger,
    exhausted_detail="Database connection pool exhausted; order was not created",
)
orders = OrderRepository(db, logger=logger, service_name=SERVICE_NAME)
tracking = OrderTrackingRepository(db)
menu_service = MenuServiceClient(logger)
user_service = UserServiceClient(logger)
restaurant_service = RestaurantServiceClient(logger)
temporal = TemporalGateway(TEMPORAL_ADDRESS, logger=logger)


@asynccontextmanager
async def lifespan(app_: FastAPI):
    """Open the database pool and the Temporal connection together.

    FastAPI takes a single lifespan, and this service now has two dependencies that need
    one. AsyncExitStack composes them without either having to know about the other, so
    `PostgresPool` stays reusable by the five services that need no orchestrator.
    """
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(db.lifespan(app_))
        await stack.enter_async_context(temporal.lifespan(app_))
        yield


app = FastAPI(title="SmartFoodOps Order Service", lifespan=lifespan)


@app.get("/api/v1/orders/health")
async def health():
    return {
        "status": "Orders Service operational",
        "service": SERVICE_NAME,
        "database_reachable": db.is_reachable(),
        # Unlike `database_reachable`, this being false does not mean the service is
        # degraded for reads: orders can still be placed and fetched. What stops is the
        # saga advancing them, which a retry repairs once the orchestrator returns.
        "temporal_reachable": await temporal.is_reachable(),
        "temporal_address": temporal.address,
        "user_service_url": user_service.base_url,
        "restaurant_service_url": restaurant_service.base_url,
        "menu_service_url": menu_service.base_url,
    }


@app.post(
    "/api/v1/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_order(
    payload: OrderCreateRequest,
    response: Response,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
    principal: Principal = Depends(require_role("customer")),
) -> OrderResponse:
    """Create an order idempotently after re-pricing it against the live menu.

    The order is placed for the token's subject. There is no way to place one for anybody
    else — `customer_id` is not a field a client can send.

    Since Week 2 this also hands the committed order to the saga, which is what carries it
    from `created` to `delivered`. Everything before that step is unchanged.
    """
    if not x_idempotency_key:
        raise bad_request("X-Idempotency-Key header is required")

    # (b) Replay protection — an already-seen key returns the stored order untouched.
    # Scoped to the caller: idempotency keys are client-chosen, so without this check a
    # guessed key would hand back somebody else's order.
    existing = orders.find_by_idempotency_key(x_idempotency_key)
    if existing is not None:
        require_self_or_admin(principal, existing["customer_id"])
        response.status_code = status.HTTP_200_OK
        logger.info("Idempotent replay for key %s", x_idempotency_key)
        # A replay also re-attempts the saga. This is what repairs an order whose workflow
        # failed to start the first time: the workflow id is derived from the order id, so
        # a saga that is already running is left alone, and one that never began now does.
        # The restaurant is re-read because a replay has none in hand — the cost of making
        # this path self-healing, paid only on an actual retry.
        await _start_saga(
            existing,
            restaurant_service.verify_restaurant(
                existing["restaurant_id"], principal.token
            ),
        )
        return OrderResponse(**existing)

    # (c) Re-price from the Menu Service; unavailable items or a total mismatch abort here.
    menu = menu_service.fetch_menu(payload.restaurant_id, principal.token)
    items_snapshot, total = build_order_snapshot(menu, payload)

    # (d) Both participants live in other services' databases, so the foreign keys that
    # used to reject an unknown id at insert time are gone. The HTTP checks that replace
    # them sit here, immediately before the write, for the same reason. The customer check
    # also outlives the token's role claim: a demoted account fails here even while holding
    # a token minted before the change.
    user_service.verify_customer(principal.user_id, principal.token)
    # The response is kept, not discarded: `capacity`, `latitude` and `longitude` are on it,
    # and handing them to the saga in its payload is what removed the saga's four HTTP calls
    # to the Restaurant Service (D32). Captured here, at checkout, from a lookup that was
    # already happening.
    restaurant = restaurant_service.verify_restaurant(
        payload.restaurant_id, principal.token
    )

    # (e) The order and the opening 'created' entry of its audit trail commit together —
    # same database, one transaction. There is no window in which one exists without the
    # other, which is what the cross-service HTTP log call could never promise.
    order = orders.create(
        payload, principal.user_id, items_snapshot, total, x_idempotency_key
    )

    # (f) The order exists; the saga runs it from here.
    await _start_saga(order, restaurant)

    return OrderResponse(**order)


async def _start_saga(order: dict, restaurant: dict) -> None:
    """Hand a committed order to the orchestrator.

    Deliberately after the commit, and deliberately not fatal.

    Temporal cannot enlist in a Postgres transaction, so "the order exists" and "its saga
    started" cannot be made one atomic fact. Given the choice, the order wins: it is what
    the customer was told about, and it is recoverable — a retry with the same idempotency
    key takes the replay branch above, which calls this again.

    That is D09's old argument reappearing in a new place, and it resolves the same way: a
    write that already succeeded must not be reported to the client as a failure.

    `USE_EXISTING` is what makes this safe to call more than once. The workflow id is
    derived from the order id, so a second attempt for one order names the same workflow
    and Temporal hands back the running one rather than raising — which is why there is no
    `except WorkflowAlreadyStartedError` here. (That exception lives in
    `temporalio.exceptions`, not `temporalio.client`, if it is ever needed.)
    """
    order_id = order["id"]
    if not temporal.connected:
        logger.error(
            "Temporal is not connected; order %s will sit at 'created' until retried",
            order_id,
        )
        return

    try:
        handle = await temporal.client.start_workflow(
            OrderWorkflow.run,
            {
                "order_id": str(order_id),
                "restaurant_id": str(order["restaurant_id"]),
                # A string, not a float: an exact decimal has to survive the JSON boundary
                # into workflow history, and D07's guarantee stops at that boundary.
                "amount": str(order["total_amount"]),
                # Snapshots taken at checkout, so the saga never has to call the Restaurant
                # Service (D32). Staleness is harmless and arguably correct: the order
                # queued under the capacity that existed when it was placed, and a
                # restaurant does not move.
                "capacity": restaurant["capacity"],
                "restaurant_latitude": restaurant["latitude"],
                "restaurant_longitude": restaurant["longitude"],
            },
            id=workflow_id_for(order_id),
            task_queue=ORDER_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        logger.info("Saga %s running for order %s", handle.id, order_id)
    except Exception as exc:  # noqa: BLE001 - the order is committed; never fail on this
        # Error rather than warning: an order with no saga stays at `created` forever
        # until something retries it, which is worth an alert even though it is not worth
        # a 500 to a client whose order was in fact created.
        logger.error("Could not start the saga for order %s: %s", order_id, exc)


@app.get("/api/v1/orders/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: UUID,
    principal: Principal = Depends(current_principal),
) -> OrderResponse:
    """Expose an order — including its server-recalculated `total_amount`.

    Added for the Payment Service: `payments.order_id` used to be a foreign key into this
    database, and this endpoint is what replaced it. The total it returns is the figure a
    payment has to match, so the authoritative amount stays owned by this service.

    Readable by the customer who placed it, or an admin. The Payment Service reaches it
    while forwarding that customer's token, so paying for an order requires being the
    person who ordered it.
    """
    row = orders.find(order_id)
    if row is None:
        raise not_found(f"Order {order_id} not found")

    require_self_or_admin(principal, row["customer_id"])
    return OrderResponse(**row)


@app.get(
    "/api/v1/orders/kitchen/{restaurant_id}",
    response_model=list[KitchenOrderResponse],
)
def kitchen_queue(
    restaurant_id: UUID,
    principal: Principal = Depends(require_role("restaurant_admin")),
) -> list[KitchenOrderResponse]:
    """The kitchen's rail: this restaurant's orders awaiting a decision, oldest first.

    Restaurant-facing, on this service, because since D32 the kitchen's queue *is* a query
    over `orders` — there is no separate ticket table to read. Whether the caller owns the
    restaurant is still the Restaurant Service's fact, resolved over HTTP (D16).

    Answers `KitchenOrderResponse`, not `OrderResponse`: an admin sees what to cook and not
    what the customer paid. Widening the queue onto `orders` deliberately did not widen what
    a restaurant can read.
    """
    restaurant_service.verify_owner(restaurant_id, principal.user_id, principal.token)
    return [
        KitchenOrderResponse(**row) for row in orders.kitchen_queue(restaurant_id)
    ]


async def _signal_saga_best_effort(order_id: UUID, signal: str, body: dict) -> None:
    """Tell the saga about something already committed, without being able to undo it.

    Best-effort on purpose. The kitchen's decision is in the database by the time this
    runs, and the admin must not see an error for something that worked — so a signal
    that cannot be delivered is logged, not raised.

    Losing it is survivable precisely because of the read-back: when the saga's timer
    expires it reads `orders.kitchen_decision` and finds the decision anyway. This is
    the one place where those two mechanisms are designed as a pair.
    """
    try:
        handle = temporal.client.get_workflow_handle(workflow_id_for(order_id))
        await handle.signal(signal, body)
        logger.info("Signalled '%s' to the saga for order %s", signal, order_id)
    except Exception as exc:  # noqa: BLE001 - the decision is committed; do not undo it
        logger.error(
            "Recorded the decision for order %s but could not signal the saga; "
            "its timeout will read the decision back instead: %s",
            order_id,
            exc,
        )


async def _decide_kitchen(
    order_id: UUID, principal: Principal, decision: str
) -> KitchenDecisionResponse:
    """Record a kitchen decision and tell the saga about it, exactly once.

    Two properties this ordering buys, and both matter:

    The decision is committed *before* the signal, so a signal that fails to send leaves a
    decision on record rather than losing it — and the saga reads that record back when its
    timer expires, which is what makes a lost signal self-correcting.

    The signal is sent only when the update actually changed something. A second accept must
    not tell the workflow twice, and a click on an order the saga already timed out and
    cancelled must not signal at all.

    Since D32 this service owns both halves: the decision is a column in its own database
    and the workflow is its own, so there is no cross-service relay left to lose.
    """
    order = orders.find(order_id)
    if order is None:
        raise not_found(f"Order {order_id} not found")
    restaurant_service.verify_owner(
        order["restaurant_id"], principal.user_id, principal.token
    )

    decided, changed = orders.decide_kitchen(order_id, decision)
    if not changed:
        logger.info(
            "Order %s is already '%s'/%s; not signalling the saga again",
            order_id,
            decided["status"],
            decided["kitchen_decision"],
        )
        return KitchenDecisionResponse(
            order_id=order_id,
            decision=decided["kitchen_decision"],
            status=decided["status"],
            changed=False,
        )

    await _signal_saga_best_effort(
        order_id, "restaurant_decision", {"decision": decision}
    )
    return KitchenDecisionResponse(
        order_id=order_id,
        decision=decided["kitchen_decision"],
        status=decided["status"],
        changed=True,
    )


@app.post(
    "/api/v1/orders/{order_id}/accept",
    response_model=KitchenDecisionResponse,
)
async def accept_order(
    order_id: UUID,
    principal: Principal = Depends(require_role("restaurant_admin")),
) -> KitchenDecisionResponse:
    """Accept an order into the kitchen, releasing the saga to find a rider."""
    return await _decide_kitchen(order_id, principal, "accepted")


@app.post(
    "/api/v1/orders/{order_id}/reject",
    response_model=KitchenDecisionResponse,
)
async def reject_order(
    order_id: UUID,
    principal: Principal = Depends(require_role("restaurant_admin")),
) -> KitchenDecisionResponse:
    """Decline an order, which makes the saga refund the customer and cancel it."""
    return await _decide_kitchen(order_id, principal, "rejected")


@app.get(
    "/api/v1/orders/{order_id}/internal",
    response_model=OrderResponse,
    dependencies=[Depends(require_internal)],
)
def get_order_internally(order_id: UUID) -> OrderResponse:
    """The same order as the endpoint above, for callers with no user behind them.

    The Payment Service's saga path needs the authoritative `total_amount` but holds no
    bearer token to forward — a workflow is not a user (D26). The ownership check the
    bearer version performs is not lost, only relocated: the saga did not choose this
    order, it was started by an already-authorised `POST /api/v1/orders` whose handler had
    established that the caller owns it.

    Internal-key only, because without the token there is no ownership check left here, so
    this must not be reachable by anyone who could guess an order id.
    """
    row = orders.find(order_id)
    if row is None:
        raise not_found(f"Order {order_id} not found")
    return OrderResponse(**row)


@app.post(
    "/api/v1/orders/{order_id}/signals",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_internal)],
)
async def signal_workflow(order_id: UUID, payload: WorkflowSignalRequest) -> dict:
    """Relay an event from a sibling service into this order's workflow.

    One endpoint rather than one per event, and internal-key only. Two consequences worth
    stating: a Temporal client exists in exactly two processes in this platform — this
    service and its worker — and the Restaurant and Rider Services stay unaware that an
    orchestrator exists at all. They report what they observed to the service that owns the
    order lifecycle, exactly as they would report any other status transition.

    No handle is stored anywhere. The workflow id is derived from the order id, so finding
    the running saga is a pure function of the thing the caller already named.

    `202`, not `200`: a signal is delivered to the workflow, not executed by it. By the time
    this returns the saga has been told, not necessarily acted.
    """
    handle = temporal.client.get_workflow_handle(workflow_id_for(order_id))
    try:
        await handle.signal(payload.signal, payload.payload)
    except RPCError as exc:
        # NOT_FOUND covers both "no such workflow" and "it already finished", and the two
        # are worth separating for the caller: a rider marking a cancelled order delivered
        # is a different problem from an order that never existed.
        if exc.status is RPCStatusCode.NOT_FOUND:
            raise not_found(
                f"Order {order_id} has no running saga to signal; it may have already "
                "finished or been cancelled"
            ) from exc
        logger.error("Could not signal saga for order %s: %s", order_id, exc)
        raise conflict(
            f"The saga for order {order_id} would not accept signal '{payload.signal}'"
        ) from exc

    logger.info("Signalled '%s' to the saga for order %s", payload.signal, order_id)
    return {"signalled": payload.signal, "order_id": str(order_id)}


@app.post(
    "/api/v1/orders/logs",
    response_model=OrderTrackingLogResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal)],
)
def log_order_status(payload: OrderTrackingLogCreateRequest) -> OrderTrackingLogResponse:
    """Append a status transition reported by another service.

    Service-to-service only, on the internal key rather than a bearer token: the Order
    Service writes its own transitions in-process, so anything arriving here is a sibling
    reporting one it observed — a rider marking a delivery, a workflow cancelling. The
    customer must not be able to call it themselves, because the audit trail cannot be
    writable by the party it is about.

    This endpoint replaced `POST /api/v1/menus/logs`; it moved with the table it writes to.

    An unknown order or an invented status is `422`: the request is well formed, and it is
    the thing it points at that is wrong.
    """
    return OrderTrackingLogResponse(**tracking.append(payload))


@app.get(
    "/api/v1/orders/{order_id}/logs",
    response_model=list[OrderTrackingLogResponse],
)
def get_order_timeline(
    order_id: UUID,
    principal: Principal = Depends(current_principal),
) -> list[OrderTrackingLogResponse]:
    """Return every recorded transition for one order, oldest first.

    Readable by the customer who placed it, or an admin — the same rule as the order
    itself, decided in the same place (D16). The order is looked up first so an unknown id
    is `404` rather than an empty list, which would otherwise be indistinguishable from an
    order that exists and has no trail.
    """
    order = orders.find(order_id)
    if order is None:
        raise not_found(f"Order {order_id} not found")

    require_self_or_admin(principal, order["customer_id"])
    return [OrderTrackingLogResponse(**row) for row in tracking.timeline(order_id)]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
