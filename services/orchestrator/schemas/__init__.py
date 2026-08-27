"""Pydantic schemas for the Orchestrator Service, one module per entity this service
orchestrates — today just `order.py`. A future second entity's request/response models get
their own `schemas/<entity>.py`, named `<Entity>Saga*` the way `order.py`'s are `OrderSaga*`.

Docstring only: no re-exports.
"""
