"""Outbound calls the order saga's activities make, one module per sibling called —
moved here verbatim from `orchestrator/clients/{order,payment,rider}.py` when clients/
was entity-scoped so a future second entity's calls (which will not be shaped like an
order's — see the module docstrings below) get their own `clients/<entity>/` instead of
crowding into this one.

order_service.py   the two calls back into the Order Service — recording a transition and
                    reading the kitchen's decision, both of them direct database access
                    before D36 moved this worker out of the Order Service's own image
payment.py         authorise and refund
rider.py           dispatch and release

Every call here carries the internal key rather than a forwarded bearer token, for the
reason D26 gives: an activity has no user behind it, and a token in a workflow argument
would be written into durable, UI-visible history.

KEEP THIS FILE A DOCSTRING. It sits on the same sandbox import path as
`orchestrator/__init__.py`, `orchestrator/workflows/__init__.py`,
`orchestrator/activities/__init__.py` and `orchestrator/clients/__init__.py` — resolving
`orchestrator.activities.order`'s own imports from inside
`workflow.unsafe.imports_passed_through()` walks through all five. A re-export here would
put every client for this entity back in the worker's import graph regardless of which
one an activity actually calls, undoing the reason `clients/` is split by callee at all.

Import from the specific module you need
(`from orchestrator.clients.order.order_service import OrderServiceClient`).
"""
