"""What this orchestrator runs: every workflow, its activities, and the queue they poll.

The one place a new workflow is declared. Before this existed, `worker.py` hardcoded
`workflows=[OrderWorkflow]` and spelled out all seven activity methods inline, so adding a
second entity meant editing the process's own startup code — and, because services reached
the saga through a REST facade back then, also writing `apis/<entity>.py` and
`schemas/<entity>.py` whose only job was to forward `start_workflow` and `signal`. Both of
those are gone: services now name a workflow by string through `common.temporal.SagaClient`,
so a new entity needs `workflows/<entity>.py`, `activities/<entity>.py`, a task-queue
constant in `common/config.py`, and one entry here. No HTTP surface at all.

Activities stay listed explicitly rather than discovered by reflection. The registered name
of an activity is part of the durable contract — a workflow already in flight looks its
activities up by name and retries forever if one has vanished — so the list is worth being
able to read, and worth breaking loudly if someone renames a method without thinking.

One `Worker` polls exactly one task queue, so each entry here becomes its own `Worker`
object in `worker.py`, all running concurrently in this single process. A second entity only
earns a second *container* if it needs to scale or deploy independently of this one.

D55 (the mentor's child-workflow split) added `PaymentWorkflow`, `RiderWorkflow` and
`CompensationWorkflow` here, all sharing `ORDER_TASK_QUEUE` rather than a queue each: they
are children `OrderWorkflow` starts and awaits directly, not independently-scheduled sagas
of their own, so there is nothing a second queue would buy beyond ceremony.
"""

from dataclasses import dataclass
from logging import Logger
from typing import Callable, Sequence

from common.config import ORDER_TASK_QUEUE
from orchestrator.activities.order import OrderActivities
from orchestrator.activities.payment import PaymentActivities
from orchestrator.activities.rider import RiderActivities
from orchestrator.clients.order.order_service import OrderServiceClient
from orchestrator.clients.payment.payment_service import PaymentServiceClient
from orchestrator.clients.rider.rider_service import SagaRiderClient
from orchestrator.workflows.compensation import CompensationWorkflow
from orchestrator.workflows.order import OrderWorkflow
from orchestrator.workflows.payment import PaymentWorkflow
from orchestrator.workflows.rider import RiderWorkflow


@dataclass(frozen=True)
class Registration:
    """One task queue and everything registered against it."""

    task_queue: str
    workflows: Sequence[type]
    activities: Sequence[Callable]


def registrations(logger: Logger) -> list[Registration]:
    """Build every workflow's collaborators and pair them with their task queue.

    Takes the logger rather than importing one because activities are constructed here, and
    an activity's HTTP clients each want the process logger — the same shape `deps.py` uses
    on the API side.
    """
    order_activities = OrderActivities(orders=OrderServiceClient(logger), logger=logger)
    payment_activities = PaymentActivities(payments=PaymentServiceClient(logger), logger=logger)
    rider_activities = RiderActivities(riders=SagaRiderClient(logger), logger=logger)

    return [
        Registration(
            task_queue=ORDER_TASK_QUEUE,
            workflows=[OrderWorkflow, PaymentWorkflow, RiderWorkflow, CompensationWorkflow],
            activities=[
                # order
                order_activities.transition_order_activity,
                order_activities.read_kitchen_decision_activity,
                # Week 3, D43 (cited as "D46" in much of the surrounding code — a
                # miscitation).
                order_activities.read_rider_report_activity,
                order_activities.create_order_activity,
                order_activities.decide_kitchen_activity,
                order_activities.publish_order_event_activity,
                # payment (PaymentWorkflow, CompensationWorkflow's refund step)
                payment_activities.authorize_payment_activity,
                payment_activities.refund_payment_activity,
                payment_activities.create_manual_payment_activity,
                payment_activities.publish_payment_event_activity,
                # rider (RiderWorkflow)
                rider_activities.dispatch_rider_activity,
                rider_activities.release_rider_activity,
            ],
        ),
    ]
