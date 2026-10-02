"""Shared order-transition and lost-signal-recovery helpers.

`OrderWorkflow` and `RiderWorkflow` each write the order's new status and publish the matching
event through the same two activities (`transition_order_activity` then
`publish_order_event_activity`); `CompensationWorkflow` did the same for its own cancellation
step inline. Previously each workflow reimplemented this pair itself. `transition_and_publish`
below is that pair, defined once.

Likewise, `OrderWorkflow`'s kitchen-decision recovery and `RiderWorkflow`'s rider-report
recovery were structurally identical: on a `wait_condition` timeout, read the Order Service's
own record once to tell a genuine timeout apart from a lost signal/Update, since both look
identical from inside the wait. `recover_via_read` below is that pattern, generalized over
which activity to call and which field/values to look for.

Reaches `orchestrator.workflows.order.activities`, so its own service-local import lives
inside `imports_passed_through()` — see `workflows/order/workflow.py`'s docstring for why.
Every workflow file that imports this module does so inside its own
`imports_passed_through()` block for the same reason (reaching this module reaches an
entity's `activities.py` transitively).
"""

from datetime import timedelta
from typing import Awaitable, Callable

from temporalio import workflow
from temporalio.exceptions import ActivityError

from orchestrator.utils.policies import PUBLISH_POLICY, STATE_POLICY

with workflow.unsafe.imports_passed_through():
    from orchestrator.workflows.order.activities import OrderActivities


async def transition_and_publish(
    order_id: str,
    new_status: str,
    updated_by: str,
    *,
    metadata: dict | None = None,
    rider_id: str | None = None,
    capacity_limit: int | None = None,
) -> None:
    """Write the order's new status, then publish the matching domain event.

    Two independently-retried activities, not one transaction — D53's replacement for the
    outbox row a status update used to write alongside itself in the same local transaction.
    """
    await workflow.execute_activity(
        OrderActivities.transition_order_activity,
        {
            "order_id": order_id,
            "status": new_status,
            "updated_by": updated_by,
            "event": {"event": f"order_{new_status}", "order_id": order_id},
            "metadata": metadata or {},
            "rider_id": rider_id,
            "capacity_limit": capacity_limit,
        },
        start_to_close_timeout=timedelta(seconds=10),
        retry_policy=STATE_POLICY,
    )
    await workflow.execute_activity(
        OrderActivities.publish_order_event_activity,
        {"order_id": order_id, "event_type": f"order.{new_status}", "metadata": metadata or {}},
        start_to_close_timeout=timedelta(seconds=10),
        retry_policy=PUBLISH_POLICY,
    )


async def recover_via_read(
    activity: Callable[..., Awaitable[dict]],
    args: dict,
    *,
    field: str,
    allowed: tuple[str, ...],
    order_id: str,
    what: str,
) -> str | None:
    """One local-database read to tell a genuine timeout apart from a lost signal/Update.

    Returns the recorded value if it's one of `allowed`, else `None` — treated as "no decision
    on record" whether the read itself fails or the field is simply unset, which is the safe
    default: a human can recover an order wrongly sent to compensation, but a charged customer
    on a saga that never finishes cannot recover itself.
    """
    try:
        recorded_on_order = await workflow.execute_activity(
            activity,
            args,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=STATE_POLICY,
        )
    except ActivityError as exc:
        workflow.logger.error(
            "Could not read the %s for order %s after the timeout; treating it as no %s: %s",
            what,
            order_id,
            what,
            exc.cause or exc,
        )
        return None

    recorded = recorded_on_order.get(field)
    if recorded in allowed:
        workflow.logger.info("Recovered a lost %s for order %s: '%s'", what, order_id, recorded)
        return recorded

    workflow.logger.info(
        "Order %s has no %s on record; the wait was genuine silence", order_id, what
    )
    return None
