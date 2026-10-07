"""`FulfillmentWorkflow` — the order's life after payment: the kitchen, the rider, and
compensation if either of them cannot finish the job (D58). Starts with the order already
`confirmed`: `OrderWorkflow` records the payment and claims the capacity slot first.

Started by `OrderWorkflow` the moment `PaymentWorkflow` authorises, with
`ParentClosePolicy.ABANDON`: the parent completes right afterwards — which is the moment the
checkout request gets its answer — and this workflow carries on without it. Splitting here
is what makes that possible: `OrderWorkflow` is now only "create, pay, hand off", so it can
end where the customer's `POST /api/v1/orders` returns, and everything after payment runs on
its own as `fulfillment-<order_id>`.

Moved almost verbatim out of `OrderWorkflow.run()`. Two things moved with it that are worth
knowing about:

  - the `kitchen_decision` Update handler. The workflow waiting on the kitchen is the one
    that has to receive its answer, so the Order Service's kitchen endpoints now address
    `fulfillment-<order_id>` (`common.temporal.fulfillment_workflow_id_for`), not
    `order-<order_id>`.
  - `_compensate`. The compensation triggers that follow the kitchen — rejection, timeout, a
    rider that never arrives — live here. `OrderWorkflow` keeps the two that come before
    fulfilment exists: payment failure (it only cancels; nothing was charged) and a full
    kitchen (it refunds through `CompensationWorkflow` itself).

`RiderWorkflow` and `CompensationWorkflow` are therefore grandchildren of `OrderWorkflow`.
Their ids are unchanged, so the Rider Service's signals still find `rider-<order_id>`.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow

from orchestrator.utils.policies import COMPENSATION_POLICY, PUBLISH_POLICY, STATE_POLICY

with workflow.unsafe.imports_passed_through():
    # Service-local imports belong inside this block — see workflows/order/workflow.py
    # for why that is a sandbox constraint and not style.
    from orchestrator.workflows.order.activities import OrderActivities
    from orchestrator.workflows.compensation.workflow import CompensationWorkflow
    from orchestrator.workflows.rider.workflow import RiderWorkflow
    from orchestrator.utils.transitions import recover_via_read
    from common.config import ORDER_TASK_QUEUE, RESTAURANT_DECISION_TIMEOUT_SECONDS
    from common.temporal import compensation_workflow_id_for, rider_workflow_id_for


@workflow.defn
class FulfillmentWorkflow:
    def __init__(self) -> None:
        self._restaurant_decision: str | None = None
        self._order_status: str | None = None
        self._rider_id: str | None = None
        self._stage = "starting"

    @workflow.update
    async def kitchen_decision(self, payload: dict) -> dict:
        """Record the kitchen's accept or reject — a Temporal Update rather than a direct
        write followed by a best-effort signal (D53). Recording the decision and unblocking
        this workflow's own wait are the same round trip.
        """
        if self._restaurant_decision is not None:
            return {
                "decision": self._restaurant_decision,
                "status": self._order_status,
                "changed": False,
            }

        order_id = payload["order_id"]
        decided = await workflow.execute_activity(
            OrderActivities.decide_kitchen_activity,
            {"order_id": order_id, "decision": payload["decision"]},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=STATE_POLICY,
        )
        self._order_status = decided.get("status")
        if not decided.get("changed"):
            return {
                "decision": decided.get("decision"),
                "status": self._order_status,
                "changed": False,
            }

        await workflow.execute_activity(
            OrderActivities.publish_order_event_activity,
            {"order_id": order_id, "event_type": "order.kitchen.decided"},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=PUBLISH_POLICY,
        )
        self._restaurant_decision = decided["decision"]
        return {"decision": decided["decision"], "status": self._order_status, "changed": True}

    @workflow.query
    def stage(self) -> dict:
        """Where fulfilment has got to — the detail `OrderWorkflow.stage` reports only as
        "fulfilling" while this child runs."""
        return {
            "stage": self._stage,
            "rider_id": self._rider_id,
            "restaurant_decision": self._restaurant_decision,
        }

    @workflow.run
    async def run(self, payload: dict) -> dict:
        order_id = payload["order_id"]

        # 1. The kitchen. The order is already 'confirmed' — on the kitchen's rail, payment
        #    recorded, capacity slot claimed — because `OrderWorkflow` does that before
        #    starting this one (D58); there is no separate "send the ticket" call, because
        #    there is no ticket (D32). So this opens straight on the wait: a durable timer
        #    for a human to answer, via the `kitchen_decision` Update (D53). The workflow is
        #    idle here — no thread, no connection, no memory in any service — and it
        #    survives a worker restart.
        self._stage = "awaiting_kitchen"
        try:
            await workflow.wait_condition(
                lambda: self._restaurant_decision is not None,
                timeout=timedelta(seconds=RESTAURANT_DECISION_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            # The timer expired without an Update. That usually means the kitchen never
            # looked at its rail — but it can also mean the kitchen *did* answer and the
            # Update was never delivered. Read the order before concluding anything:
            # refunding an order the kitchen actually accepted is a real customer-visible
            # failure, avoidable with one local lookup.
            self._stage = "recovering_kitchen_decision"
            self._restaurant_decision = await recover_via_read(
                OrderActivities.read_kitchen_decision_activity,
                {"order_id": order_id},
                field="decision",
                allowed=("accepted", "rejected"),
                order_id=order_id,
                what="kitchen decision",
            )

            if self._restaurant_decision is None:
                return await self._compensate(
                    order_id,
                    "kitchen_timeout",
                    f"No decision within {RESTAURANT_DECISION_TIMEOUT_SECONDS}s",
                )

        if self._restaurant_decision == "rejected":
            return await self._compensate(
                order_id, "kitchen_rejected", "Restaurant declined the order"
            )

        # 2. Rider dispatch, pickup, delivery — a child workflow (D55) that owns its own
        #    retry loop, its own signals, and its own cleanup on every exit path.
        self._stage = "dispatching_rider"
        rider_result = await workflow.execute_child_workflow(
            RiderWorkflow.run,
            {
                "order_id": order_id,
                "restaurant_latitude": payload["restaurant_latitude"],
                "restaurant_longitude": payload["restaurant_longitude"],
            },
            id=rider_workflow_id_for(order_id),
            task_queue=ORDER_TASK_QUEUE,
        )
        if rider_result["status"] != "delivered":
            return await self._compensate(
                order_id, rider_result["reason"], rider_result.get("detail", "")
            )

        self._rider_id = rider_result.get("rider_id")
        self._stage = "delivered"
        return self._result("delivered")

    # --- helpers ------------------------------------------------------------------------

    def _result(self, status: str, reason: str | None = None) -> dict:
        """What `OrderWorkflow` reads back: the outcome plus what its `stage` query reports."""
        result = {
            "status": status,
            "rider_id": self._rider_id,
            "restaurant_decision": self._restaurant_decision,
        }
        if reason is not None:
            result["reason"] = reason
        return result

    async def _compensate(self, order_id: str, reason: str, detail: str) -> dict:
        """Start `CompensationWorkflow` as a child and wait for its result (D55).

        `retry_policy` here is a *workflow*-level retry — on top of the activity-level
        retries already inside `CompensationWorkflow` itself — so a failure that outlasts
        those (a Payment Service outage longer than the activity's own retry window, say)
        still gets a few full attempts at the whole compensation flow before this gives up
        for good. Safe to retry wholesale: `refund_payment_activity` is idempotent by the
        payment's own status, not a caller-supplied key, so starting over never double-
        refunds.
        """
        self._stage = f"compensating:{reason}"
        result = await workflow.execute_child_workflow(
            CompensationWorkflow.run,
            {"order_id": order_id, "reason": reason, "detail": detail},
            id=compensation_workflow_id_for(order_id),
            task_queue=ORDER_TASK_QUEUE,
            retry_policy=COMPENSATION_POLICY,
        )
        # `result["status"]` rather than a hardcoded "cancelled": compensation does not
        # always end in a clean cancellation — see CompensationWorkflow's own docstring for
        # the 'compensation_failed' outcome.
        self._stage = f"{result['status']}:{reason}"
        return self._result(result["status"], reason)
