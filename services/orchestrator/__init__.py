"""SmartFoodOps Orchestrator Service — Temporal workflows and their workers, split out of
the services whose lifecycles they orchestrate (Port 8007) (D36). Today that is only the
`order` entity; `activities/`, `clients/` and `workflows/` are all entity-scoped so a
second one adds files beside `order`'s rather than growing it, and `registry.py` is the one
place it has to be declared. `apis/` holds only `health.py` now: D47 deleted the saga routes
and the schemas that fed them, because services name workflows by string through
`common.temporal.SagaClient` rather than posting to this service.

A package rather than loose modules so its own modules import absolutely
(`from orchestrator.activities.order import ...`) and cannot be shadowed by a top-level
dependency of the same name. The Dockerfile copies it to /app/orchestrator/.

KEEP THIS FILE A DOCSTRING. Do not add imports or re-exports.

Temporal's workflow sandbox imports `orchestrator.workflows.order`, and importing a
submodule imports its parent package first — so this file is executed *inside* the
sandbox, outside the `workflow.unsafe.imports_passed_through()` block in
`workflows/order.py` that makes the rest of the import graph safe. A single convenience
re-export here would drag the FastAPI app, deps.py and `required("INTERNAL_API_KEY")` in
with it — the same failure `order/__init__.py` was written to prevent, inherited verbatim
because the constraint did not change when the worker moved, only which package it lives
in. `workflows/__init__.py`, `activities/__init__.py`, `clients/__init__.py` and
`clients/order/__init__.py` all carry the same warning, for the same reason — see their
own docstrings for exactly which import chain puts each of them on the sandbox's path.

The failure is not a clean error, which is why this warning is here rather than in a review
checklist: the sandbox rejects the import, Temporal retries the workflow task forever, and
the order simply never advances past 'created'.
"""
