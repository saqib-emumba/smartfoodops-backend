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

from clients import SagaPaymentClient, SagaRiderClient
from repository import AtCapacity, OrderRepository


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
        try:
            order, changed = self._orders.transition(
                order_id=details["order_id"],
                new_status=details["status"],
                updated_by=details.get("updated_by", "order-workflow"),
                event=details.get("event"),
                metadata=details.get("metadata"),
                rider_id=details.get("rider_id"),
                capacity_limit=details.get("capacity_limit"),
            )
        except AtCapacity as exc:
            # A full kitchen is the restaurant's answer, not a malfunction. Non-retryable:
            # asking again cannot change a refusal already given, and the saga compensates.
            raise ApplicationError(str(exc), non_retryable=True) from exc

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

    # --- kitchen ---------------------------------------------------------------------

    @activity.defn
    def read_kitchen_decision_activity(self, details: dict) -> dict:
        """Read the kitchen's answer straight off the order.

        Called when the saga's wait for a decision times out. That wait can expire for two
        very different reasons — the kitchen ignored the order, or it answered and the signal
        never landed — and refunding the second case is a real customer-visible failure.

        Since D32 this is a local read of `orders.kitchen_decision` rather than an HTTP call
        into another service, which removes the failure mode the old version had to guess
        around: "the Restaurant Service is unreachable so we cannot tell" is no longer one of
        the possible answers.
        """
        order_id = details["order_id"]
        order = self._orders.find(order_id)
        if order is None:
            raise ApplicationError(
                f"Order {order_id} no longer exists", non_retryable=True
            )
        decision = order.get("kitchen_decision")
        self._logger.info(
            "Kitchen decision for order %s reads '%s'", order_id, decision
        )
        return {"decision": decision, "status": order["status"]}

    # --- fleet --------------------------------------------------------------------------

    @activity.defn
    def dispatch_rider_activity(self, details: dict) -> dict:
        """One attempt at claiming the nearest rider.

        The coordinates arrive in `details` rather than being fetched. `create_order`
        already reads the restaurant to verify it exists, so the latitude and longitude are
        in hand at checkout and ride in the workflow payload — which is what took the saga's
        HTTP calls to the Restaurant Service from four to zero (D32).

        An empty fleet returns `{"assigned": false}` rather than raising. Whether to wait
        and try again is a scheduling decision, and scheduling belongs to the workflow,
        which can sleep on a durable timer; an activity can only fail.
        """
        order_id = details["order_id"]
        result = self._riders.dispatch(
            order_id,
            float(details["restaurant_latitude"]),
            float(details["restaurant_longitude"]),
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
