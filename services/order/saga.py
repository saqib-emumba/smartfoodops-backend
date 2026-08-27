"""Handing an order to the orchestrator, and telling it about events afterward.

Two functions, kept in one module because they are two halves of one mechanism: a signal
that fails to send is recoverable only because the workflow reads the same fact back when
its timer expires — see the docstring on _signal_saga_best_effort below. Splitting them
across files would separate the two things that have to be understood together.

Request-path only. workflows.py, activities.py and worker.py never import this module —
they reach Temporal through their own mechanisms, and the saga's outbound HTTP calls run
through clients/payment.py and clients/rider.py instead.
"""

from uuid import UUID

from temporalio.common import WorkflowIDConflictPolicy

from common.config import ORDER_TASK_QUEUE
from common.temporal import workflow_id_for
from order import deps
from order.workflows import OrderWorkflow


async def start_saga(order: dict, restaurant: dict) -> None:
    """Hand a committed order to the orchestrator.

    Deliberately after the commit, and deliberately not fatal.

    Temporal cannot enlist in a Postgres transaction, so "the order exists" and "its saga
    started" cannot be made one atomic fact. Given the choice, the order wins: it is what
    the customer was told about, and it is recoverable — a retry with the same idempotency
    key takes the replay branch in api/checkout.py, which calls this again.

    That is D09's old argument reappearing in a new place, and it resolves the same way: a
    write that already succeeded must not be reported to the client as a failure.

    `USE_EXISTING` is what makes this safe to call more than once. The workflow id is
    derived from the order id, so a second attempt for one order names the same workflow
    and Temporal hands back the running one rather than raising — which is why there is no
    `except WorkflowAlreadyStartedError` here. (That exception lives in
    `temporalio.exceptions`, not `temporalio.client`, if it is ever needed.)
    """
    order_id = order["id"]
    if not deps.temporal.connected:
        deps.logger.error(
            "Temporal is not connected; order %s will sit at 'created' until retried",
            order_id,
        )
        return

    try:
        handle = await deps.temporal.client.start_workflow(
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
        deps.logger.info("Saga %s running for order %s", handle.id, order_id)
    except Exception as exc:  # noqa: BLE001 - the order is committed; never fail on this
        # Error rather than warning: an order with no saga stays at `created` forever
        # until something retries it, which is worth an alert even though it is not worth
        # a 500 to a client whose order was in fact created.
        deps.logger.error("Could not start the saga for order %s: %s", order_id, exc)


async def signal_saga_best_effort(order_id: UUID, signal: str, body: dict) -> None:
    """Tell the saga about something already committed, without being able to undo it.

    Best-effort on purpose. The kitchen's decision is in the database by the time this
    runs, and the admin must not see an error for something that worked — so a signal
    that cannot be delivered is logged, not raised.

    Losing it is survivable precisely because of the read-back: when the saga's timer
    expires it reads `orders.kitchen_decision` and finds the decision anyway. This is
    the one place where those two mechanisms are designed as a pair.
    """
    try:
        handle = deps.temporal.client.get_workflow_handle(workflow_id_for(order_id))
        await handle.signal(signal, body)
        deps.logger.info("Signalled '%s' to the saga for order %s", signal, order_id)
    except Exception as exc:  # noqa: BLE001 - the decision is committed; do not undo it
        deps.logger.error(
            "Recorded the decision for order %s but could not signal the saga; "
            "its timeout will read the decision back instead: %s",
            order_id,
            exc,
        )
