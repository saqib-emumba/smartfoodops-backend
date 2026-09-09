"""Routers for the Orchestrator Service. Only one, since D47.

health.py  the liveness probe, reporting whether Temporal answers

There is deliberately no `apis/<entity>.py`. This package once held `order.py`, whose two
routes forwarded to `start_workflow` and `handle.signal` and did nothing else; services now
hold their own Temporal client (`common.temporal.SagaClient`) and name workflows by string,
so a new orchestrated entity adds no route here — it adds a workflow, its activities, and a
line in `registry.py`.

Docstring only: no re-exports, so main.py names the module it wants.
"""
