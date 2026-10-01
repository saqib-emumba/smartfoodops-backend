"""`PaymentWorkflow`'s and `CompensationWorkflow`'s calls into the Payment Service (D53/
outbox-removal-temporal-design.md ss4b, unified across saga/manual/refund by D55).

A sibling of `clients/order/`, not a member of it: this is the "payment" entity's own client,
for a workflow that has nothing to do with the order saga — D30's direct/manual payment
endpoint, pulled inside Temporal only because removing the outbox left it with no activity to
chain a publish step off of otherwise.

KEEP THIS FILE A DOCSTRING. It sits on the same sandbox import path as
`orchestrator/__init__.py`, `orchestrator/workflows/__init__.py` and
`orchestrator/activities/__init__.py` — resolving `orchestrator.activities.payment`'s own
imports from inside `workflow.unsafe.imports_passed_through()` walks through all four, for
the same reason `clients/order/__init__.py` documents at length. A re-export here would put
the order saga's clients back in this workflow's import graph regardless of which one it
actually calls.

Import from the specific module you need
(`from orchestrator.clients.payment.payment_service import PaymentServiceClient`).
"""
