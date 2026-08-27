"""SmartFoodOps Order Service — idempotent checkout (Port 8004).

A package rather than loose modules so its own modules import absolutely
(`from order.repositories.orders import ...`) and cannot be shadowed by a top-level
dependency of the same name. The Dockerfile copies it to /app/order/.

Docstring only, holding to the D34 convention every service's top package follows — not
because of the Temporal sandbox any more. Before D36 this file carried an urgent warning:
Temporal's workflow sandbox imported `order.workflows`, which meant importing this package
*inside* the sandbox, outside the `imports_passed_through()` block that made the rest of the
import graph safe, and a re-export here would have dragged the FastAPI app and
`required("DATABASE_URL")` into it. `order.workflows` does not exist any more — it moved to
`orchestrator/workflows.py` along with the worker that runs it — so that specific danger now
belongs to `orchestrator/__init__.py` instead. See its docstring.
"""
