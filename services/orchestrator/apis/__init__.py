"""Routers for the Orchestrator Service — `health.py`, plus one module per entity this
service orchestrates.

health.py  the liveness probe
order.py   start the order saga, and relay a signal into one — both internal-key only

A future second entity's routes live in their own `apis/<entity>.py`, matching
`schemas/<entity>.py` and `activities/<entity>.py`.

Docstring only: no re-exports, so main.py names the module it wants.
"""
