"""The `fulfillment` entity: `FulfillmentWorkflow` (`workflow.py`) — everything that happens
to an order after its payment has authorised (D58). No `activities.py` of its own: the
kitchen's decision and its read-back are facts about the *order* record, so it calls into
`order/activities.py`, the same way `compensation/` does.

KEEP THIS FILE A DOCSTRING, for the same sandbox-import reason `workflows/order/__init__.py`
states in full.
"""
