"""Repository layer for the `orders` table and the `order_tracking_logs` trail beside it.

sql.py       the raw SQL, and _append_log — the seam both repositories below write through
orders.py    AtCapacity + OrderRepository, the `orders` table
tracking.py  OrderTrackingRepository, the append-only trail

Docstring only: no re-exports. Both `order/worker.py` and `order/activities/activities.py`
import from this package, and the latter does so from inside workflows.py's
`imports_passed_through()` block — the same reasoning that keeps `order/__init__.py` and
`order/clients/__init__.py` free of imports applies here, even though nothing here is
domain-sensitive: a docstring-only `__init__.py` is one rule held everywhere rather than an
exception carved out for the modules that happen not to need it yet.
"""
