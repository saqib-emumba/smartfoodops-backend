"""Temporal activities for the order saga — one module, `OrderActivities`, unsplit.

Wrapped in a package purely for naming consistency with clients/, schemas/ and
repositories/ elsewhere in this service; `OrderActivities` is deliberately NOT split by
domain the way those are. Temporal records every `@activity.defn` method's name in durable
workflow history, and a class split risks a rename during the refactor — any workflow
started before such a deploy would then fail to find its registered activity, retried
forever. The file is already sectioned internally by domain (state, payment, kitchen,
fleet); only its location changed here, never its shape (D34).

Docstring only: no re-exports. `workflows.py` imports
`from order.activities.activities import OrderActivities` from inside
`imports_passed_through()`, exactly as it already does for `order.repositories.orders` and
`order.clients.payment`/`.rider` — this package's `__init__.py` is passed through the same
way theirs is, and must stay empty for the same reason theirs does.
"""
