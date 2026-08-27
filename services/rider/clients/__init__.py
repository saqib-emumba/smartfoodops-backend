"""Outbound calls to sibling services, one module per sibling called.

Two different credentials are used across them, and the difference is the point:

* The **User Service** lookup (user.py) runs as the rider, forwarding their bearer token,
  so this service can never read more about an account than the account holder could (D15).
* The **Order Service** signal relay (order.py) runs on the internal key. A pickup is a
  fact this service observed, and the workflow it feeds is not something an end user may
  poke — the same reasoning that keeps the audit trail internal-only.

Docstring only: no re-exports here. Routers and deps.py import from the specific module
they need (`from rider.clients.user import UserServiceClient`).
"""
