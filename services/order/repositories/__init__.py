"""Repository layer for the `orders` table and the `order_tracking_logs` trail beside it.

sql.py       the raw SQL, and _append_log — the seam both repositories below write through
orders.py    AtCapacity + OrderRepository, the `orders` table
tracking.py  OrderTrackingRepository, the append-only trail

Docstring only: no re-exports. Before D36 this mattered for a sandbox reason —
`order/worker.py` and `order/activities/activities.py` both imported from here, the latter
from inside workflows.py's `imports_passed_through()` block. Both moved to
`services/orchestrator/` along with the Temporal client this service no longer holds, and
`AtCapacity` now crosses the boundary too, thrown by `apis/transitions.py` and caught by
`orchestrator/activities/activities.py` instead of caught in-process. The rule survives the
reason that first motivated it: a docstring-only `__init__.py` held everywhere is cheaper
than an exception carved out for the modules that happen not to need it any more.
"""
