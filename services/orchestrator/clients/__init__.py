"""Outbound calls this service's activities make, one subdirectory per entity — today just
`order/`, holding the order saga's three clients (moved here verbatim from
`order/clients/payment.py` and `order/clients/rider.py`, plus one new module, when D36
split the worker out of the Order Service).

A future second entity gets its own `clients/<entity>/` beside `order/`, even where its
calls reach the same sibling service `order/` already calls: `order/order_service.py`,
`order/payment.py` and `order/rider.py` are all shaped around `order_id` specifically (see
their own docstrings), so a different entity's calls to Payment or Rider would need
different methods, not a shared client. See `clients/order/__init__.py` for the callee
list and the internal-key rationale.

Docstring only: no re-exports, at this level or the entity level below it. Import from
the specific module you need
(`from orchestrator.clients.order.order_service import OrderServiceClient`) — this
package's `__init__.py` sits on the same sandbox import path as `orchestrator/__init__.py`
and `orchestrator/activities/__init__.py`, and a re-export here would put every entity's
every client back in the worker's import graph regardless of which one an activity
actually calls.
"""
