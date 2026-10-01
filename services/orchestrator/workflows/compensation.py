"""`CompensationWorkflow` — undo a saga that cannot proceed (D55, the mentor's
child-workflow split).

Started as a child of `OrderWorkflow` whenever forward progress stops: the kitchen's rail is
full, it never answers, it rejects the order, or `RiderWorkflow` reports it could not
complete (no rider found, or a pickup/delivery that never happened). Refunds the payment,
then cancels the order — in that order, because the refund is the customer's money.

Deliberately never touches a rider. `RiderWorkflow` releases whatever it claimed on every one
of its own exit paths before this workflow ever starts (see workflows/rider.py) — either a
rider was never assigned (the kitchen never got that far), or it has already been released.
Keeping that cleanup inside `RiderWorkflow` itself, rather than passing a `rider_id` here for
this workflow to release, is what lets this workflow stay a pure "undo the money and the
order" concern with no fleet awareness at all.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities.order import OrderActivities
    from orchestrator.activities.payment import PaymentActivities

# Compensations retry harder than forward progress, and the asymmetry is deliberate: a
# failed refund leaves a customer charged for an order that will never arrive, which is the
# worst state this system can be in. Better to keep trying for minutes than to give up.
COMPENSATION = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=10,
)

STATE = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
)

PUBLISH = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
)


@workflow.defn
class CompensationWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        order_id = payload["order_id"]
        reason = payload["reason"]
        detail = payload.get("detail", "")

        workflow.logger.info("Compensating order %s: %s (%s)", order_id, reason, detail)

        refund = await workflow.execute_activity(
            PaymentActivities.refund_payment_activity,
            {"order_id": order_id, "reason": reason},
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=COMPENSATION,
        )
        await workflow.execute_activity(
            PaymentActivities.publish_payment_event_activity,
            {"event_type": "payment.refunded", "payment": refund},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=PUBLISH,
        )

        # Nothing frees the kitchen's capacity slot explicitly, because nothing has to: the
        # rail is defined as `status = 'confirmed' AND kitchen_decision IS NULL` (D32), so
        # the transition below removes this order from it as a side effect of being
        # cancelled.
        await workflow.execute_activity(
            OrderActivities.transition_order_activity,
            {
                "order_id": order_id,
                "status": "cancelled",
                "updated_by": "order-workflow",
                "event": {"event": "order_cancelled", "order_id": order_id},
                "metadata": {"reason": reason, "detail": detail[:500]},
                "rider_id": None,
                "capacity_limit": None,
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=STATE,
        )
        await workflow.execute_activity(
            OrderActivities.publish_order_event_activity,
            {
                "order_id": order_id,
                "event_type": "order.cancelled",
                "metadata": {"reason": reason, "detail": detail[:500]},
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=PUBLISH,
        )

        return {"status": "cancelled", "order_id": order_id, "reason": reason}
