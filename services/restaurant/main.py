"""SmartFoodOps Restaurant Service — onboarding, lookups and the kitchen queue (Port 8002).

Owns the `restaurants` table in its own PostgreSQL database, and since Week 2 the
`order_tickets` queue beside it. Owner identity/authorisation is resolved over HTTP against
the User Service, whose database this service cannot reach.

The queue exists because the order saga has to wait for a human. The first revision of the
Week 2 blueprint modelled acceptance as a synchronous HTTP call that returned the kitchen's
decision, which no real kitchen can do — so a ticket is recorded here, an admin decides at
their own pace, and the decision is relayed into the workflow as a signal (D27).

Whether a caller owns a restaurant is this service's fact, so the authorisation for a
decision is settled here and nowhere else (D16).
"""

from uuid import UUID

from fastapi import Depends, FastAPI, status

from clients import OrderServiceClient, UserServiceClient
from common.auth import (
    Principal,
    current_principal,
    require_internal,
    require_role,
    require_self_or_admin,
)
from common.config import required
from common.errors import not_found, unprocessable
from common.logging_config import configure_logging
from common.postgres import PostgresPool
from repository import RestaurantRepository, TicketRepository
from schemas import (
    RestaurantOnboardRequest,
    RestaurantResponse,
    TicketCreateRequest,
    TicketCreateResponse,
    TicketResponse,
)

SERVICE_NAME = "restaurant-service"
DATABASE_URL = required("DATABASE_URL")

logger = configure_logging(SERVICE_NAME)
db = PostgresPool(DATABASE_URL, logger=logger)
restaurants = RestaurantRepository(db)
tickets = TicketRepository(db)
user_service = UserServiceClient(logger)
order_service = OrderServiceClient(logger)

app = FastAPI(title="SmartFoodOps Restaurant Service", lifespan=db.lifespan)


def _owned_restaurant(restaurant_id: UUID, principal: Principal) -> dict:
    """The restaurant, if this caller owns it.

    Ownership is checked before anything is revealed about the restaurant, so a stranger's
    id and a nonexistent one look the same from outside — the same reasoning that makes an
    unknown user id a 403 rather than a 404 (D17).
    """
    row = restaurants.find(restaurant_id)
    if row is None:
        raise not_found(f"Restaurant {restaurant_id} not found")
    require_self_or_admin(principal, row["owner_id"])
    return row


@app.get("/api/v1/restaurants/health")
def health():
    return {
        "status": "Restaurant Service is operational",
        "service": SERVICE_NAME,
        "database_reachable": db.is_reachable(),
        "user_service_url": user_service.base_url,
        "order_service_url": order_service.base_url,
    }


@app.post(
    "/api/v1/restaurants/onboard",
    response_model=RestaurantResponse,
    status_code=status.HTTP_201_CREATED,
)
def onboard_restaurant(
    payload: RestaurantOnboardRequest,
    principal: Principal = Depends(require_role("restaurant_admin")),
) -> RestaurantResponse:
    """Onboard a restaurant once its owner is verified through the User Service.

    The owner is the token's subject, so a restaurant can only ever be onboarded under the
    account making the request.
    """
    user_service.verify_owner(principal.user_id, principal.token)
    return RestaurantResponse(**restaurants.onboard(payload, principal.user_id))


@app.get("/api/v1/restaurants/{restaurant_id}", response_model=RestaurantResponse)
def get_restaurant(
    restaurant_id: UUID,
    _: Principal = Depends(current_principal),
) -> RestaurantResponse:
    """Expose restaurant state (including is_active) for other services to verify.

    Any authenticated caller: customers browsing and the Order and Menu Services checking
    a restaurant all read the same non-sensitive record.
    """
    row = restaurants.find(restaurant_id)
    if row is None:
        raise not_found(f"Restaurant {restaurant_id} not found")
    return RestaurantResponse(**row)


@app.get(
    "/api/v1/restaurants/{restaurant_id}/internal",
    response_model=RestaurantResponse,
    dependencies=[Depends(require_internal)],
)
def get_restaurant_internally(restaurant_id: UUID) -> RestaurantResponse:
    """The same record as the endpoint above, for callers with no user behind them.

    The saga's dispatch step needs the restaurant's coordinates, and an activity holds no
    bearer token to satisfy `current_principal` — a token in a workflow argument would be
    durable history and would expire mid-saga anyway (D26). Same data, different
    credential; nothing here is sensitive, which is why the bearer version admits any
    authenticated caller too.
    """
    row = restaurants.find(restaurant_id)
    if row is None:
        raise not_found(f"Restaurant {restaurant_id} not found")
    return RestaurantResponse(**row)


@app.post(
    "/api/v1/restaurants/tickets",
    response_model=TicketCreateResponse,
    dependencies=[Depends(require_internal)],
)
def create_ticket(payload: TicketCreateRequest) -> TicketCreateResponse:
    """Present an order to a kitchen.

    Service-to-service only, on the internal key: a customer does not put tickets on a
    kitchen's rail, and the caller is a workflow with no bearer token to forward (D26).

    Idempotent on `order_id`, because Temporal retries this activity — a resent ticket
    returns the existing one rather than queueing the order twice or, worse, resetting a
    decision the kitchen has already made.

    A full kitchen answers `200 {"queued": false, "reason": "at_capacity"}`. That is the
    first thing in the platform to read `restaurants.capacity`, which has been a column
    nothing consulted since Week 1. It is deliberately not an error status: being full is
    an answer the saga compensates for, and a retry cannot change it.
    """
    restaurant = restaurants.find(payload.restaurant_id)
    if restaurant is None:
        raise unprocessable(
            f"Unknown restaurant {payload.restaurant_id}; there is nothing to queue against"
        )

    ticket, outcome = tickets.enqueue(
        payload.order_id,
        payload.restaurant_id,
        payload.items,
        restaurant["capacity"],
    )

    if outcome == "at_capacity":
        logger.info(
            "Restaurant %s is at capacity (%s); refused order %s",
            payload.restaurant_id,
            restaurant["capacity"],
            payload.order_id,
        )
        return TicketCreateResponse(
            queued=False, order_id=payload.order_id, reason="at_capacity"
        )

    if outcome == "already_queued":
        logger.info("Order %s is already on the rail", payload.order_id)

    return TicketCreateResponse(
        queued=True,
        order_id=payload.order_id,
        reason=outcome,
        ticket=TicketResponse(**ticket),
    )


@app.get(
    "/api/v1/restaurants/{restaurant_id}/tickets",
    response_model=list[TicketResponse],
)
def list_tickets(
    restaurant_id: UUID,
    ticket_status: str = "pending",
    principal: Principal = Depends(require_role("restaurant_admin")),
) -> list[TicketResponse]:
    """The kitchen's queue, oldest first — what an admin reads before deciding."""
    _owned_restaurant(restaurant_id, principal)
    return [TicketResponse(**row) for row in tickets.queue(restaurant_id, ticket_status)]


@app.get(
    "/api/v1/restaurants/tickets/{order_id}",
    response_model=TicketResponse,
    dependencies=[Depends(require_internal)],
)
def get_ticket(order_id: UUID) -> TicketResponse:
    """Read one ticket by the order it belongs to.

    Internal-key only, and it exists for exactly one caller: the saga, when its wait for a
    kitchen decision times out. A decision is committed here *before* the signal relaying it
    is sent, so a relay that fails in flight leaves a ticket that says `accepted` and a
    workflow that never heard about it. Rather than refund a customer whose order the
    kitchen did in fact take, the saga asks this endpoint what the ticket actually says.

    Keyed by `order_id` rather than the ticket's own id because that is what the saga
    knows — and `order_id` is `UNIQUE` on this table, so it identifies exactly one row.

    A `404` here is a real answer, not a failure: no ticket was ever queued for this order.
    """
    ticket = tickets.find(order_id)
    if ticket is None:
        raise not_found(f"No ticket for order {order_id}")
    return TicketResponse(**ticket)


@app.post(
    "/api/v1/restaurants/tickets/{order_id}/expire",
    response_model=TicketResponse,
    dependencies=[Depends(require_internal)],
)
def expire_ticket(order_id: UUID) -> TicketResponse:
    """Retire a ticket whose order the saga has cancelled.

    Internal-key only: this is a compensating action, not something a kitchen or a customer
    does. It exists because the capacity count filters on `status = 'pending'`, so a ticket
    left behind by a cancelled order would hold a slot in that kitchen's queue forever —
    and enough of them would make the restaurant permanently unable to accept anything.

    Idempotent, and it cannot overwrite a real decision: only `pending` rows move. A ticket
    the kitchen accepted a moment before the saga gave up stays `accepted`, and a ticket
    that never existed is a no-op rather than a `404`, because a compensation that fails
    for having nothing to do would be retried until the workflow gave up.
    """
    expired = tickets.expire(order_id)
    if expired is not None:
        logger.info("Expired the pending ticket for order %s", order_id)
        return TicketResponse(**expired)

    existing = tickets.find(order_id)
    if existing is None:
        raise unprocessable(f"Order {order_id} has no ticket to expire")
    logger.info(
        "Ticket for order %s is already '%s'; nothing to expire",
        order_id,
        existing["status"],
    )
    return TicketResponse(**existing)


def _decide(order_id: UUID, principal: Principal, decision: str) -> TicketResponse:
    """Record a kitchen decision and relay it to the saga exactly once.

    The relay happens after the update and only when the update changed something. A second
    accept must not signal the workflow twice, and signalling before committing would risk
    telling the saga about a decision that then failed to persist.

    A signal that cannot be delivered is *not* rolled back: the ticket is decided, and the
    kitchen should not see an error for something they did successfully. The saga's own
    timeout is the backstop if the signal is lost — which is the one hole this design keeps,
    and it is recorded as such.
    """
    ticket = tickets.find(order_id)
    if ticket is None:
        raise not_found(f"No ticket for order {order_id}")
    _owned_restaurant(ticket["restaurant_id"], principal)

    decided, changed = tickets.decide(order_id, decision)
    if not changed:
        logger.info(
            "Ticket for order %s was already %s; not signalling again",
            order_id,
            decided["status"],
        )
        return TicketResponse(**decided)

    try:
        order_service.signal(
            order_id, "restaurant_decision", {"decision": decision}
        )
    except Exception as exc:  # noqa: BLE001 - the decision is committed; do not undo it
        logger.error(
            "Recorded %s for order %s but could not signal the saga: %s",
            decision,
            order_id,
            exc,
        )
    return TicketResponse(**decided)


@app.post(
    "/api/v1/restaurants/tickets/{order_id}/accept",
    response_model=TicketResponse,
)
def accept_ticket(
    order_id: UUID,
    principal: Principal = Depends(require_role("restaurant_admin")),
) -> TicketResponse:
    """Accept an order into the kitchen, releasing the saga to find a rider."""
    return _decide(order_id, principal, "accepted")


@app.post(
    "/api/v1/restaurants/tickets/{order_id}/reject",
    response_model=TicketResponse,
)
def reject_ticket(
    order_id: UUID,
    principal: Principal = Depends(require_role("restaurant_admin")),
) -> TicketResponse:
    """Decline an order, which makes the saga refund the customer and cancel it."""
    return _decide(order_id, principal, "rejected")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
