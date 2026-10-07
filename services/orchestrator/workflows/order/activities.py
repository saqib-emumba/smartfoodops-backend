"""Temporal activities for the `order` entity's saga.

The non-deterministic half of `OrderWorkflow`: HTTP calls to the Order Service, since D36
moved this worker out of its image and database. See
orchestrator/clients/order/order_service.py for what replaced the direct access.

D55 (mentor's child-workflow split) moved payment and rider activities out of this class
entirely — `workflows/payment/activities.py` and `workflows/rider/activities.py` now back
`PaymentWorkflow` and `RiderWorkflow`, each a child of `OrderWorkflow`. What remains here is
what is genuinely about the `order` entity's own state: creating it, transitioning it, and
publishing its events. `read_rider_report_activity` stays here rather than moving to
`workflows/rider/activities.py` because it reads `orders.rider_reported_stage` through
`OrderServiceClient`, not anything the Rider Service owns — a fact about the order, read
back by `RiderWorkflow`'s own recovery logic across the entity boundary, the same way
`read_kitchen_decision_activity` is read across it by `FulfillmentWorkflow` (D58).

This module — like `clients/order/` and `workflow.py` beside it — is scoped to the `order`
entity specifically, so a future second entity this service orchestrates gets its own
`workflows/<entity>/activities.py` beside this one rather than a second class crammed in
here, and declares it in `registry.py`.

Two rules decide the shape of every function below, unchanged by any of the moves above.

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
from typing import Callable

from fastapi import HTTPException
from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.clients.order.order_service import (
    AtCapacity,
    OrderCreateConflict,
    OrderCreateRejected,
    OrderGone,
    OrderServiceClient,
)


class OrderActivities:
    """Activity implementations, bound to one set of clients.

    A class rather than module functions so the worker can build the clients once at
    startup instead of per activity execution, and so `activity.defn`'s default name
    derivation — `ClassName.method_name` never appears; Temporal names an activity by its
    *method*, and `OrderActivities` supplies the shared state each method needs.

    Constructed in `registry.py`, which is where the method list Temporal registers lives.
    """

    def __init__(self, *, orders: OrderServiceClient, logger: Logger):
        self._orders = orders
        self._logger = logger

    def all_activities(self) -> list[Callable]:
        """Every activity Temporal should register for this class — read by `registry.py`,
        not discovered: an activity's registered name is part of Temporal's durable
        contract, so this list is kept explicit rather than reflected, the same reasoning
        `registry.py`'s own docstring gives in full."""
        return [
            self.transition_order_activity,
            self.read_kitchen_decision_activity,
            # Week 3, D43 (cited as "D46" in much of the surrounding code — a miscitation).
            self.read_rider_report_activity,
            self.create_order_activity,
            self.decide_kitchen_activity,
            self.publish_order_event_activity,
        ]

    # --- creation (D54/order-creation-temporal-update-design.md) -------------------------

    @activity.defn
    def create_order_activity(self, payload: dict) -> dict:
        """Step 1 of `OrderWorkflow.run()` (D58; an Update handler's write before that).

        `OrderInternalCreateRequest` on the wire does the re-pricing, verification and
        insert `checkout.py` used to do directly — see
        order/apis/internal_orders.py. A rejection (unavailable item, price mismatch,
        unknown restaurant) or a genuine cross-customer idempotency-key collision are both
        business answers, not transport failures, so both are non-retryable: asking again
        cannot change either.
        """
        try:
            return self._orders.create(payload)
        except OrderCreateRejected as exc:
            # `type=` is what the client-side unwrap in `common.temporal` uses to pick the
            # right HTTP status back out from underneath Temporal's ActivityError/
            # WorkflowUpdateFailedError wrapping — see `common.temporal._raise_mapped`.
            raise ApplicationError(str(exc), type="OrderCreateRejected", non_retryable=True) from exc
        except OrderCreateConflict as exc:
            raise ApplicationError(str(exc), type="OrderCreateConflict", non_retryable=True) from exc
        except HTTPException as exc:
            # Anything `ServiceClient` answered with a real status this method has no more
            # specific exception for — a 403 from a sibling's ownership check, a 404 for "no
            # menu published yet". Still a business answer, not a transport failure, so still
            # non-retryable — tagged generically by status rather than needing a named
            # exception type per code every downstream call could plausibly return.
            raise ApplicationError(
                str(exc.detail), type=f"http_{exc.status_code}", non_retryable=True
            ) from exc

    @activity.defn
    def decide_kitchen_activity(self, details: dict) -> dict:
        """The write behind `FulfillmentWorkflow.kitchen_decision`'s Update handler — see
        order/apis/internal_kitchen.py. Returns `{order_id, decision, status, changed}`;
        `changed` is what lets the Update handler decide whether to publish."""
        order_id = details["order_id"]
        try:
            return self._orders.decide_kitchen(order_id, details["decision"])
        except OrderGone as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        except HTTPException as exc:
            raise ApplicationError(
                str(exc.detail), type=f"http_{exc.status_code}", non_retryable=True
            ) from exc

    # --- publishing (D53: replaces order_outbox and its relay) --------------------------

    @activity.defn
    def publish_order_event_activity(self, details: dict) -> None:
        """Tell Kafka about a fact this saga's writes already committed.

        Reads the order fresh rather than trusting a caller-supplied snapshot: by the time
        this runs, the write activity chained before it (or the Update handler this shares a
        caller with) has already committed whatever this event describes, so a fresh read
        is simpler than threading the same fields through two activities' worth of
        arguments. `event_id` is derived from `(order_id, event_type)`
        (`common.kafka.deterministic_event_id`), so retrying this activity after it already
        reached Kafka once republishes the identical event rather than a new one — every
        consumer already dedups on it (D39's contract, unchanged by D53).
        """
        order_id = details["order_id"]
        event_type = details["event_type"]
        order = self._orders.read(order_id)
        if event_type == "order.created":
            data = {
                "order_id": order_id,
                "customer_id": order["customer_id"],
                "restaurant_id": order["restaurant_id"],
                "total_amount": order["total_amount"],
                "status": order["status"],
                "items_count": len(order.get("items", [])),
            }
        elif event_type == "order.kitchen.decided":
            data = {
                "order_id": order_id,
                "decision": order["kitchen_decision"],
                "restaurant_id": order["restaurant_id"],
            }
        else:
            data = {
                "order_id": order_id,
                "new_status": order["status"],
                "customer_id": order["customer_id"],
                "restaurant_id": order["restaurant_id"],
                "rider_id": order.get("rider_id"),
                "total_amount": order["total_amount"],
                "metadata": details.get("metadata") or {},
            }
        self._orders.publish_event(order_id, event_type=event_type, data=data)

    # --- state ---------------------------------------------------------------------------

    @activity.defn
    def transition_order_activity(self, details: dict) -> dict:
        """Move the order to a new status and append the transition.

        Delegates to `OrderServiceClient.transition`, an HTTP call into the endpoint that
        does the compare-and-set and derives `old_status` from the preceding trail entry
        rather than accepting it from the workflow (D24) — the same guarantee
        `OrderRepository.transition` made when this was a direct database write. A replay
        changes nothing and writes no duplicate entry.

        Returns the resulting status rather than a bare bool so the workflow can tell a
        real transition from a no-op without a second read.
        """
        order_id = details["order_id"]
        try:
            return self._orders.transition(
                order_id,
                status=details["status"],
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
        except OrderGone as exc:
            # The order is gone. Nothing a retry can fix, and the saga cannot continue —
            # so fail it outright rather than looping until the retry policy gives up.
            raise ApplicationError(str(exc), non_retryable=True) from exc

    # --- kitchen ---------------------------------------------------------------------

    @activity.defn
    def read_kitchen_decision_activity(self, details: dict) -> dict:
        """Read the kitchen's answer straight off the order.

        Called when the saga's wait for a decision times out. That wait can expire for two
        very different reasons — the kitchen ignored the order, or it answered and the
        Update never arrived — and refunding the second case is a real customer-visible
        failure.

        Since D32 this is a read of `orders.kitchen_decision`, and since D36 that read
        crosses an HTTP boundary it did not used to: the worker no longer shares a database
        with the Order Service. The internal read endpoint it hits is the same one the
        request path already relies on for the saga hand-off, so nothing new opened for
        this — see `orchestrator/clients/order/order_service.py::read`.
        """
        order_id = details["order_id"]
        try:
            order = self._orders.read(order_id)
        except OrderGone as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        decision = order.get("kitchen_decision")
        self._logger.info(
            "Kitchen decision for order %s reads '%s'", order_id, decision
        )
        return {"decision": decision, "status": order["status"]}

    @activity.defn
    def read_rider_report_activity(self, details: dict) -> dict:
        """Read what the rider has reported straight off the order (Week 3, D46).

        The same recovery `read_kitchen_decision_activity` already gives the kitchen's
        answer, for the other signal a lost relay can strand: a pickup or delivery report
        committed to `orders.rider_reported_stage` before the signal that carries it into
        `RiderWorkflow` (D55), so a timeout can tell "the rider genuinely never reported"
        apart from "they reported and the signal never landed."
        """
        order_id = details["order_id"]
        try:
            order = self._orders.read(order_id)
        except OrderGone as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        stage = order.get("rider_reported_stage")
        self._logger.info(
            "Rider report for order %s reads '%s'", order_id, stage
        )
        return {"stage": stage}
