"""Routers for the Order Service, one module per domain area.

checkout.py   POST /orders and its two reads — the customer-facing lifecycle
kitchen.py    the queue and the accept/reject decision — restaurant-facing
signals.py    the one relay endpoint a sibling service posts an event into
tracking.py   the append-only audit trail

Docstring only: no re-exports, so main.py names the router it wants.
"""
