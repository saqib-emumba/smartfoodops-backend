"""`RiderWorkflow`'s calls into the Rider Service — dispatch and release (D55, the
mentor's child-workflow split; moved verbatim out of `clients/order/rider.py` when the
rider entity's saga logic earned its own workflow rather than living inside `OrderWorkflow`).

KEEP THIS FILE A DOCSTRING. Same sandbox-import reasoning `clients/order/__init__.py`
documents at length: this sits on the same import path
`orchestrator.workflows.rider.activities`'s own imports resolve through inside
`workflow.unsafe.imports_passed_through()`.

Import from the specific module you need
(`from orchestrator.clients.rider.rider_service import SagaRiderClient`).
"""
