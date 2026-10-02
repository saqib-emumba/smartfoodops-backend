"""The `rider` entity: `RiderWorkflow` (`workflow.py`) and `RiderActivities` (`activities.py`)
live beside each other here rather than in a separate top-level `activities/` directory,
since nothing outside this saga's own workflow ever needs to find one without the other.

KEEP THIS FILE A DOCSTRING, for the same sandbox-import reason `workflows/order/__init__.py`
states in full.
"""
