"""Temporal activities for the order saga.

The non-deterministic half of the workflow: HTTP calls to sibling services, and writes to
this service's own database. Everything here is a **sync** function, executed by the worker
on a thread pool, so the saga reaches Postgres through the same `PostgresPool` the request
handlers use rather than a second async engine alongside it (D21).

Two rules decide the shape of every function below.

**Every activity is idempotent.** Temporal guarantees at-least-once execution, not
exactly-once: a worker that dies after calling a service but before recording the result
will call it again. So every key is *derived* from the order id rather than generated, and
every write is guarded — a retry has to be a no-op, not a second charge or a second rider.

**A business outcome is not a failure.** Temporal retries every exception except
`ApplicationError(non_retryable=True)`, so a declined card or a kitchen refusing an order
must be raised non-retryably. The first revision of the Week 2 blueprint raised a plain
`ValueError` for a rejection under a 3-attempt retry policy, which re-sent the order to the
restaurant three times before compensating. Transport failures raise normally, so those —
and only those — get retried.

Every call outward carries `X-Internal-Key` rather than a forwarded bearer token. A workflow
argument is durable, UI-visible history, so a bearer token must never be one; and a
15-minute access token cannot outlive a saga that waits on a kitchen (D26).
"""

from logging import Logger

from temporalio import activity
from temporalio.exceptions import ApplicationError

from clients import SagaPaymentClient, SagaRestaurantClient, SagaRiderClient
from repository import OrderRepository


def payment_key(order_id: str) -> str:
    """The idempotency key for this order's authorisation.

    Derived, never generated. A retried activity must present the key of the attempt it is
    replacing, or the unique index that prevents double charging never sees a collision and
    the customer pays twice.
    """
    return f"wf-pay-{order_id}"


class OrderActivities:
    """Activity implementations, bound to one repository and one set of clients.

    A class rather than module functions so the worker can hand in the same repository the
    request handlers use, and so the HTTP clients are built once at startup instead of per
    activity execution.
    """

    def __init__(self, *, orders: OrderRepository, logger: Logger):
        self._orders = orders
        self._logger = logger
        self._payments = SagaPaymentClient(logger)
        self._restaurants = SagaRestaurantClient(logger)
        self._riders = SagaRiderClient(logger)

    # --- state ---------------------------------------------------------------------------

    @activity.defn
    def transition_order_activity(self, details: dict) -> dict:
        """Move the order to a new status and append the transition, in one transaction.

        Delegates to `OrderRepository.transition`, which does the compare-and-set and
        derives `old_status` from the preceding trail entry rather than accepting it from
        the workflow (D24). A replay changes nothing and writes no duplicate entry.

        Returns the resulting status rather than a bare bool so the workflow can tell a
        real transition from a no-op without a second read.
        """
        order, changed = self._orders.transition(
            order_id=details["order_id"],
            new_status=details["status"],
            updated_by=details.get("updated_by", "order-workflow"),
            event=details.get("event"),
            metadata=details.get("metadata"),
            rider_id=details.get("rider_id"),
        )

        if order is None:
            # The order is gone. Nothing a retry can fix, and the saga cannot continue —
            # so fail it outright rather than looping until the retry policy gives up.
            raise ApplicationError(
                f"Order {details['order_id']} no longer exists",
                non_retryable=True,
            )

        return {"status": order["status"], "changed": changed}

    # --- payment ------------------------------------------------------------------------

    @activity.defn
    def authorize_payment_activity(self, details: dict) -> dict:
        """Charge the card.

        A declined card is final; an unreachable Payment Service is not. The Payment
        Service answers `422` for an amount that does not settle the order and `409` for an
        order already paid — both arrive here as a `bad_gateway`/`unprocessable` HTTPException
        from ServiceClient, which is a *retryable* exception by default. That is wrong for a
        decline, so the status of the returned payment is what this checks: anything other
        than `authorized` is a non-retryable failure.
        """
        order_id = details["order_id"]
        payment = self._payments.authorize(
            order_id, details["amount"], payment_key(order_id)
        )

        if payment.get("status") != "authorized":
            raise ApplicationError(
                f"Payment for order {order_id} came back '{payment.get('status')}' "
                "rather than authorized",
                non_retryable=True,
            )
        self._logger.info("Authorised payment for order %s", order_id)
        return payment

    @activity.defn
    def refund_payment_activity(self, details: dict) -> dict:
        """Compensating action: release a hold the saga can no longer honour.

        Carries no idempotency key of its own because the Payment Service makes this
        idempotent by *status* — a payment already `refunded` is returned untouched. That is
        the safer guarantee for money: a key can be lost, but the row's state cannot.
        """
        order_id = details["order_id"]
        refunded = self._payments.refund(order_id, details.get("reason", "saga_failure"))
        self._logger.info(
            "Refund for order %s resolved as '%s'", order_id, refunded.get("status")
        )
        return refunded

    # --- kitchen ------------------------------------------------------------------------

    @activity.defn
    def send_ticket_activity(self, details: dict) -> dict:
        """Put the order on the kitchen's rail.

        Returning successfully means the ticket is queued, *not* that it was accepted — the
        kitchen answers later, by signal. A full kitchen is a non-retryable failure: waiting
        and asking again would only hold the customer longer for an order this restaurant
        has already refused capacity for.
        """
        order_id = details["order_id"]
        result = self._restaurants.send_ticket(
            order_id, details["restaurant_id"], details.get("items", [])
        )

        if not result.get("queued"):
            raise ApplicationError(
                f"Restaurant {details['restaurant_id']} refused order {order_id}: "
                f"{result.get('reason', 'unknown')}",
                non_retryable=True,
            )
        return result

    @activity.defn
    def expire_ticket_activity(self, details: dict) -> dict:
        """Retire the kitchen ticket for an order the saga is abandoning.

        Part of compensation, and easy to overlook: the capacity check counts `pending`
        tickets, so a ticket left behind by a cancelled order occupies a slot in that
        kitchen's queue permanently. Enough of them and the restaurant can no longer accept
        anything — a slow leak with the same shape as the rider leak, in a different table.

        Never raises for "nothing to expire": the Restaurant Service answers `422` only when
        no ticket ever existed, and a compensation that failed for having nothing to do
        would be retried until the workflow gave up.
        """
        order_id = details["order_id"]
        try:
            result = self._restaurants.expire_ticket(order_id)
        except Exception as exc:  # noqa: BLE001 - see below
            # A ticket that cannot be expired is a capacity slot lost, not an order left
            # wrong. Log it and let the rest of the rollback proceed; failing here would
            # block the refund that actually matters to the customer.
            self._logger.error("Could not expire ticket for order %s: %s", order_id, exc)
            return {"expired": False}
        self._logger.info(
            "Ticket for order %s is now '%s'", order_id, result.get("status")
        )
        return result

    # --- fleet --------------------------------------------------------------------------

    @activity.defn
    def dispatch_rider_activity(self, details: dict) -> dict:
        """One attempt at claiming the nearest rider.

        Reads the restaurant for its coordinates first — the first revision of this
        blueprint hardcoded a latitude and longitude, and the columns are `NOT NULL` and
        already on the response.

        An empty fleet returns `{"assigned": false}` rather than raising. Whether to wait
        and try again is a scheduling decision, and scheduling belongs to the workflow,
        which can sleep on a durable timer; an activity can only fail.
        """
        order_id = details["order_id"]
        restaurant = self._restaurants.fetch_restaurant(details["restaurant_id"])

        result = self._riders.dispatch(
            order_id,
            float(restaurant["latitude"]),
            float(restaurant["longitude"]),
        )
        if not result.get("assigned"):
            self._logger.info(
                "No rider available for order %s (%s)",
                order_id,
                result.get("reason", "unknown"),
            )
        return result

    @activity.defn
    def release_rider_activity(self, details: dict) -> dict:
        """Return a claimed rider to the pool.

        Called on delivery *and* on every compensation path. The first revision of this
        blueprint had no such activity, so a rider claimed by a saga that later failed
        stayed `is_available = FALSE` permanently — the fleet drained one failed order at a
        time. Idempotent: releasing an order nobody holds is success.
        """
        order_id = details["order_id"]
        released = self._riders.release(order_id)
        self._logger.info(
            "Release for order %s: %s", order_id, released.get("released")
        )
        return released
