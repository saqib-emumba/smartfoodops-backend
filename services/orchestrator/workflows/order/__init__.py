"""The `order` entity: `OrderWorkflow` (`workflow.py`) and `OrderActivities` (`activities.py`)
live beside each other here rather than in a separate top-level `activities/` directory,
since nothing outside this saga's own workflows ever needs to find one without the other.

`OrderActivities` is not order-exclusive, though: `RiderWorkflow` reads a rider report back
through it, and `CompensationWorkflow` transitions the order through it, because both facts
are genuinely about the *order* record even when triggered by another entity's saga — see
`activities.py`'s own docstring. Those two import this package from outside, the same way
they would have reached a top-level `activities/order.py` before this move.

KEEP THIS FILE A DOCSTRING. It sits on the same sandbox import path
`orchestrator/__init__.py` and `orchestrator/workflows/__init__.py` already do: Temporal's
sandbox imports `orchestrator.workflows.order` before `workflow.py`'s own
`imports_passed_through()` block runs, and importing `orchestrator.workflows.order.activities`
from `rider/workflow.py` or `compensation/workflow.py` imports this file first too. A re-export here
would execute under sandbox restriction and fail the workflow task forever — see
`orchestrator/__init__.py` for the full chain this constraint was first documented against.
"""
