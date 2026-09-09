"""Pydantic v2 schemas for the Order Service, grouped by the area they serve.

orders.py       line items, checkout request/response — the customer-facing shape
kitchen.py      the narrower projection a restaurant admin sees, and its decision outcome
rider_reports.py  the stage the Rider Service records before signalling the saga itself
tracking.py     the append-only audit trail, in both directions
transitions.py  the orchestrator's one write into this service (D36)

Coupling worth knowing: OrderResponse and KitchenOrderResponse both depend on
OrderItemSnapshot (orders.py) — a kitchen still needs to see what was ordered, just not
what was paid. The two tracking models depend on nothing outside tracking.py.

Docstring only: no re-exports. `from order.schemas.orders import OrderResponse`, not
`from order.schemas import OrderResponse` — the latter would need this file to know every
model in every submodule, which is exactly the flat file being split.
"""
