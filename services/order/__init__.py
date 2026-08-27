"""SmartFoodOps Order Service — checkout and the order saga (Port 8004).

A package rather than loose modules so its own modules import absolutely
(`from order.repositories.orders import ...`) and cannot be shadowed by a top-level
dependency of the same name. The Dockerfile copies it to /app/order/.

KEEP THIS FILE A DOCSTRING. Do not add imports or re-exports.

Temporal's workflow sandbox imports `order.workflows`, and importing a submodule
imports its parent package first — so this file is executed *inside* the sandbox,
outside the `workflow.unsafe.imports_passed_through()` block in workflows.py that
makes the rest of the import graph safe. A single convenience re-export here would
drag the FastAPI app, deps.py and `required("DATABASE_URL")` in with it.

The failure is not a clean error, which is why this warning is here rather than in
a review checklist: the sandbox rejects the import, Temporal retries the workflow
task forever, and the order simply never advances past 'created'. The smoke suite
reports it as `order reaches 'confirmed'` timing out, which points at the Payment
Service. You would debug the wrong service.
"""
