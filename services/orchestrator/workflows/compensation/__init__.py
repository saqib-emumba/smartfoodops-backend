"""The `compensation` entity: `CompensationWorkflow` (`workflow.py`), on its own in a
package even though it has no `activities.py` of its own — it calls into
`workflows/order/activities.py` and `workflows/payment/activities.py` instead (see
`workflow.py`'s own docstring for why it deliberately never touches a rider). Kept as a
package rather than a flat module for the same reason `order/`, `payment/` and `rider/`
are: one shape for every entity `registry.py` declares, not an exception for the one that
happens to own no activities.

KEEP THIS FILE A DOCSTRING, for the same sandbox-import reason `workflows/order/__init__.py`
states in full.
"""
