"""What this orchestrator runs: every workflow, its activities, and the queue they poll.

The one place a new workflow is declared. Before this existed, `worker.py` hardcoded
`workflows=[OrderWorkflow]` and spelled out all seven activity methods inline, so adding a
second entity meant editing the process's own startup code — and, because services reached
the saga through a REST facade back then, also writing `apis/<entity>.py` and
`schemas/<entity>.py` whose only job was to forward `start_workflow` and `signal`. Both of
those are gone: services now name a workflow by string through `common.temporal.SagaClient`,
so a new entity needs `workflows/<entity>/workflow.py`, `workflows/<entity>/activities.py`,
a task-queue constant in `common/config.py`, and one entry here. No HTTP surface at all.

Activities stay listed explicitly rather than discovered by reflection. The registered name
of an activity is part of the durable contract — a workflow already in flight looks its
activities up by name and retries forever if one has vanished — so the list is worth being
able to read, and worth breaking loudly if someone renames a method without thinking. Each
entity's own `activities.py` owns its slice of that list (`all_activities()` on its class),
right beside the methods it enumerates, so adding or removing one is a single edit rather
than a second, easy-to-forget one here. This file only concatenates the slices per task
queue — see `OrderActivities.all_activities`'s docstring for the full reasoning.

One `Worker` polls exactly one task queue, so each entry here becomes its own `Worker`
object in `worker.py`, all running concurrently in this single process. A second entity only
earns a second *container* if it needs to scale or deploy independently of this one.

D55 (the mentor's child-workflow split) added `PaymentWorkflow`, `RiderWorkflow` and
`CompensationWorkflow` here, all sharing `ORDER_TASK_QUEUE` rather than a queue each: they
are children `OrderWorkflow` starts and awaits directly, not independently-scheduled sagas
of their own, so there is nothing a second queue would buy beyond ceremony. D58's
`FulfillmentWorkflow` joins them on the same queue for the same reason.
"""

from dataclasses import dataclass
from logging import Logger
from typing import Callable, Sequence

from common.config import ORDER_TASK_QUEUE
from orchestrator.clients.order.order_service import OrderServiceClient
from orchestrator.clients.payment.payment_service import PaymentServiceClient
from orchestrator.clients.rider.rider_service import SagaRiderClient
from orchestrator.workflows.compensation.workflow import CompensationWorkflow
from orchestrator.workflows.fulfillment.workflow import FulfillmentWorkflow
from orchestrator.workflows.order.activities import OrderActivities
from orchestrator.workflows.order.workflow import OrderWorkflow
from orchestrator.workflows.payment.activities import PaymentActivities
from orchestrator.workflows.payment.workflow import PaymentWorkflow
from orchestrator.workflows.rider.activities import RiderActivities
from orchestrator.workflows.rider.workflow import RiderWorkflow


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
            workflows=[
                OrderWorkflow,
                PaymentWorkflow,
                FulfillmentWorkflow,
                RiderWorkflow,
                CompensationWorkflow,
            ],
            activities=[
                *order_activities.all_activities(),
                *payment_activities.all_activities(),
                *rider_activities.all_activities(),
            ],
        ),
    ]
