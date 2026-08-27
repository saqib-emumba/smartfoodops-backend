"""Temporal activities, one module per entity this service orchestrates — today just
`order.py`, holding `OrderActivities` unsplit.

`OrderActivities` is deliberately NOT split by domain the way `clients/order/` is.
Temporal records every `@activity.defn` method's name in durable workflow history, and a
class split risks a rename during the refactor — any workflow started before such a deploy
would then fail to find its registered activity, retried forever. Moving the whole module
from `order/activities/activities.py` to `orchestrator/activities/activities.py` (D36),
and later to `orchestrator/activities/order.py` when this package was made entity-scoped,
changed its imports and, inside two methods, what a call actually does; the class name and
all six method names travelled unchanged both times, because that is the one thing about
this file a move is not allowed to touch.

A future second entity gets its own `activities/<entity>.py` beside `order.py` — never a
second class inside it.

Docstring only: no re-exports. `workflows/order.py` imports
`from orchestrator.activities.order import OrderActivities` from inside
`imports_passed_through()`, exactly as `order/workflows.py` did for
`order.activities.activities` before D36 moved this package out of the Order Service —
this package's `__init__.py` is passed through the same way, and must stay empty for the
same reason.
"""
