"""Outbound calls `OrderWorkflow` and its own activities make — the `order` entity's own
client, since D55 moved payment and rider calls out into `clients/payment/` and
`clients/rider/` alongside their own workflows.

order_service.py   creating an order, recording a transition, the kitchen's decision, and
                    reading the order back — all of them direct database access before D36
                    moved this worker out of the Order Service's own image

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
