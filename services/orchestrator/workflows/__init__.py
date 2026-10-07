"""Temporal workflow definitions, one subpackage per entity this service orchestrates —
`order/` (`OrderWorkflow`, the parent), and, since D55's child-workflow split, `payment/`
(`PaymentWorkflow`), `rider/` (`RiderWorkflow`) and `compensation/` (`CompensationWorkflow`).
D58 added `fulfillment/` (`FulfillmentWorkflow`): `OrderWorkflow` runs `PaymentWorkflow` as a
child, then starts `FulfillmentWorkflow` abandoned and completes; `FulfillmentWorkflow`
starts the rider and compensation children in turn.

Each entity subpackage holds its workflow (`workflow.py`) and, where it has one, the
activities backing it (`activities.py`) side by side — moved here from a separate top-level
`activities/` directory, since nothing outside a saga's own workflows ever needed to find
one without the other. `compensation/` has no `activities.py` of its own (it calls into
`order/activities.py` and `payment/activities.py` instead) but still gets the same package
shape as the other three, rather than being the one flat-module exception — see
`compensation/__init__.py`. `utils/` still holds what's genuinely cross-entity (retry
policies, the transition/recovery helpers), per its own docstring.

KEEP THIS FILE A DOCSTRING. Temporal's sandbox imports `orchestrator.workflows.order`,
and importing a submodule imports its parent package first — so this file executes
*inside* the sandbox, before `workflow.unsafe.imports_passed_through()` in
`workflows/order/workflow.py` is even reached. A re-export here would put whatever it
imports under sandbox restriction and fail the workflow task forever. See
`orchestrator/__init__.py` for the same constraint one level up, and
`workflows/order/__init__.py` and `workflows/order/workflow.py`'s own docstrings for the
full chain.

A future second orchestrated entity (not a child of `OrderWorkflow`) gets its own
`workflows/<entity>/` beside these — never a second top-level `workflows.py`, and never
anything in this `__init__.py` but a docstring.
"""
