"""The `payment` entity: `PaymentWorkflow` (`workflow.py`) and `PaymentActivities`
(`activities.py`) live beside each other here rather than in a separate top-level
`activities/` directory, since nothing outside this saga's own workflow ever needs to find
one without the other. `CompensationWorkflow` reaches `PaymentActivities` from outside for
its own refund step, the same way it would have reached a top-level `activities/payment.py`
before this move.

KEEP THIS FILE A DOCSTRING, for the same sandbox-import reason `workflows/order/__init__.py`
states in full.
"""
