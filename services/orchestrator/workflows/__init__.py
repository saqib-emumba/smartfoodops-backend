"""Temporal workflow definitions, one module per entity this service orchestrates —
`order.py` (`OrderWorkflow`, the parent), and, since D55's child-workflow split,
`payment.py` (`PaymentWorkflow`), `rider.py` (`RiderWorkflow`) and `compensation.py`
(`CompensationWorkflow`), each started as a child and awaited by `OrderWorkflow` itself.

KEEP THIS FILE A DOCSTRING. Temporal's sandbox imports `orchestrator.workflows.order`,
and importing a submodule imports its parent package first — so this file executes
*inside* the sandbox, before `workflow.unsafe.imports_passed_through()` in
`workflows/order.py` is even reached. A re-export here would put whatever it imports
under sandbox restriction and fail the workflow task forever. See
`orchestrator/__init__.py` for the same constraint one level up, and
`workflows/order.py`'s own docstring for the full chain.

A future second orchestrated entity (not a child of `OrderWorkflow`) gets its own
`workflows/<entity>.py` beside these — never a second top-level `workflows.py`, and never
anything in this `__init__.py` but a docstring.
"""
