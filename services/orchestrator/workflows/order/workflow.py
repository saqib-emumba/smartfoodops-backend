"""The order's checkout as a durable workflow: create, pay, hand off (D58).

Deterministic by construction: every side effect is an activity or a child workflow, and
every wait is a Temporal timer. Nothing here reads a clock, opens a socket or touches a
database. That is what lets the worker be killed mid-saga and resume exactly where it
stopped, which is the property the whole of Week 2 exists to demonstrate.

The checkout API starts this workflow with the customer's cart as its start argument, sends
nothing into it, and waits for its *result*. `run()` does the work in order:

  1. create the order (`create_order_activity`) and publish `order.created`;
  2. authorise payment (`PaymentWorkflow`, a child — D55);
  3. mark the order `confirmed` — the transition that records the payment on the order and
     claims a slot on the kitchen's rail, in one local transaction;
  4. start `FulfillmentWorkflow`, which owns the kitchen's decision, the rider and any later
     compensation, and return the order.

Step 4 starts that child with `ParentClosePolicy.ABANDON`: this workflow completes
straight afterwards — that completion is the API's answer — and the child carries on without
it. Steps 1 to 3 are all settled before the child exists, so an order is never on the
kitchen's rail without its payment recorded. A failure at any of them fails this workflow
with an `ApplicationError` whose `type` the API maps onto a status
(`common.temporal._raise_mapped`): a rejected cart is a `422`/`404`/`409`, a declined payment
`402`, a full kitchen `409` (after the refund).

So `order-<id>` means "checkout", not "the whole order": the kitchen and rider progress is on
`fulfillment-<id>` and below. Before D58 a `create_order` Update did step 1 itself while
`run()` waited on it, and this workflow stayed open until delivery; the mentors' direction
was that creating an order should just start the workflow, with no Update and nothing to poll.

Read alongside `activities.py` beside this file: the division of labour is that activities
decide what a *service* said, and this file decides what the *saga* does about it.

Lives at `workflows/order/workflow.py`, not a flat `workflows/order.py`, so each entity this
service orchestrates gets its own `workflows/<entity>/` holding both its workflow and the
activities backing it — see `workflows/__init__.py` and `workflows/order/__init__.py` for
why that package boundary carries the same sandbox constraint this file's own import block
does.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from orchestrator.utils.constants import DETAIL_TRUNCATE_LENGTH, PAYMENT_MODE_SAGA
from orchestrator.utils.policies import COMPENSATION_POLICY, PUBLISH_POLICY, STATE_POLICY

with workflow.unsafe.imports_passed_through():
    # Every service-local import belongs inside this block, and the reason is not
    # style. Reaching `activities` pulls in `clients`, and through them `common.auth`,
    # which calls `required("JWT_PUBLIC_KEY_B64")` at import time. Passed through, the
    # sandbox reuses the already-loaded modules; outside the block it would re-execute
    # them under restriction and fail the workflow task — forever, since Temporal
    # retries it. Add imports here, never above.
    from orchestrator.workflows.order.activities import OrderActivities
    from orchestrator.workflows.compensation.workflow import CompensationWorkflow
    from orchestrator.workflows.fulfillment.workflow import FulfillmentWorkflow
    from orchestrator.workflows.payment.workflow import PaymentWorkflow
    from orchestrator.utils.transitions import transition_and_publish
    from common.config import ORDER_TASK_QUEUE
    from common.temporal import (
        compensation_workflow_id_for,
        fulfillment_workflow_id_for,
        payment_workflow_id_for,
    )


@workflow.defn
class OrderWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        # 1. Create the order. A rejection (bad cart, unknown restaurant, key collision) is
        #    a business answer from a non-retryable activity: nothing was written, so there
        #    is nothing to cancel — only an answer to give back, as this workflow's failure.
        try:
            order = await workflow.execute_activity(
                OrderActivities.create_order_activity,
                payload,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=STATE_POLICY,
            )
        except ActivityError as exc:
            cause = exc.cause if isinstance(exc.cause, ApplicationError) else None
            raise ApplicationError(
                cause.message if cause else str(exc),
                type=cause.type if cause else None,
                non_retryable=True,
            ) from exc

        order_id = order["order"]["id"]
        if not order["created"]:
            # The order already existed, so an earlier run of this workflow id owns its saga
            # — a checkout retried after that run finished, which starts a fresh run under
            # the same id because a closed workflow cannot be joined. Answer with the order
            # as it stands and stop: carrying on would authorise the same order's payment
            # twice.
            return order

        await workflow.execute_activity(
            OrderActivities.publish_order_event_activity,
            {"order_id": order_id, "event_type": "order.created"},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=PUBLISH_POLICY,
        )

        # 2. Payment — a child workflow (D55). Nothing has been charged if this fails, so a
        #    failure here needs no compensation, only a cancellation.
        try:
            await workflow.execute_child_workflow(
                PaymentWorkflow.run,
                {
                    "order_id": order_id,
                    "amount": str(order["order"]["total_amount"]),
                    "mode": PAYMENT_MODE_SAGA,
                },
                id=payment_workflow_id_for(order_id),
                task_queue=ORDER_TASK_QUEUE,
            )
        except (ChildWorkflowError, ActivityError) as exc:
            await self._cancel(order_id, "payment_failed", str(exc.cause or exc))
            raise ApplicationError(
                f"Payment for order {order_id} was not authorised; the order has been "
                "cancelled",
                type="PaymentDeclined",
                non_retryable=True,
            ) from exc

        # 3. Confirm — record the payment on the order and join the kitchen's rail. Entering
        #    'confirmed' *is* both: this single transition writes the payment's effect and
        #    claims a capacity slot in one local transaction, so two orders cannot both take
        #    the last one. It happens here, before fulfilment exists, so the order is never
        #    on the rail unpaid. A full kitchen comes back as a non-retryable failure: the
        #    customer has already been charged, so refund and cancel before answering.
        try:
            await transition_and_publish(
                order_id, "confirmed", "payment-service", capacity_limit=order["capacity"]
            )
        except ActivityError as exc:
            compensated = await self._compensate(
                order_id, "kitchen_at_capacity", str(exc.cause or exc)
            )
            refunded = compensated["status"] == "cancelled"
            raise ApplicationError(
                "The kitchen is at capacity; the order has been cancelled"
                + (" and the payment refunded" if refunded else " — the refund is being "
                   "retried and will need attention if it does not complete"),
                type="KitchenAtCapacity",
                non_retryable=True,
            ) from exc

        # 4. Fulfilment — the kitchen's decision, the rider, and compensation if either
        #    fails (D58). *Started*, not awaited, and abandoned: this workflow completes
        #    right after, and that completion is the checkout API's answer. Awaiting the
        #    start (not the result) is what guarantees the child exists before this one
        #    closes.
        await workflow.start_child_workflow(
            FulfillmentWorkflow.run,
            {
                "order_id": order_id,
                "restaurant_latitude": order["restaurant_latitude"],
                "restaurant_longitude": order["restaurant_longitude"],
            },
            id=fulfillment_workflow_id_for(order_id),
            task_queue=ORDER_TASK_QUEUE,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        return order

    # --- helpers ------------------------------------------------------------------------

    async def _compensate(self, order_id: str, reason: str, detail: str) -> dict:
        """Refund and cancel via `CompensationWorkflow`, for the one trigger that can happen
        before fulfilment starts: the kitchen being full. Same child, same id and same
        workflow-level retry as `FulfillmentWorkflow._compensate`; at most one of the two ever
        runs for an order, since this path returns before fulfilment is started."""
        return await workflow.execute_child_workflow(
            CompensationWorkflow.run,
            {"order_id": order_id, "reason": reason, "detail": detail},
            id=compensation_workflow_id_for(order_id),
            task_queue=ORDER_TASK_QUEUE,
            retry_policy=COMPENSATION_POLICY,
        )

    async def _cancel(self, order_id: str, reason: str, detail: str) -> None:
        """Mark the order cancelled. No money moved, so nothing to give back — this is the
        one cancellation path that does not go through `CompensationWorkflow`."""
        await transition_and_publish(
            order_id,
            "cancelled",
            "order-workflow",
            metadata={"reason": reason, "detail": detail[:DETAIL_TRUNCATE_LENGTH]},
        )
