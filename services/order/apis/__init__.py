"""Routers for the Order Service, one module per domain area.

health.py       the liveness probe, registered first — see its own docstring for why
checkout.py     POST /orders and its two reads — the customer-facing lifecycle
kitchen.py      the queue and the accept/reject decision — restaurant-facing
rider_reports.py  the pickup/delivery stage the Rider Service records here (was signals.py
                until D47, when the relay half moved to the Rider Service's own client)
tracking.py     the append-only audit trail
transitions.py  the one write the orchestrator makes into this service (D36)

Docstring only: no re-exports, so main.py names the router it wants.
"""
