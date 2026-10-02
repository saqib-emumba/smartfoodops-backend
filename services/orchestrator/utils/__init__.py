"""Shared support code for this service's Temporal workflows — retry policies, small
constants, and the transition/recovery helpers every workflow in `workflows/` calls into.

Not a general dumping ground: everything here exists because more than one module under
`workflows/` needed the exact same thing (see each module's own docstring for which). A
future addition that only one workflow needs belongs in that workflow's own file instead.

Also not `orchestrator/common/`: `services/common/` already exists, shared across every
microservice in this repo (`common.config`, `common.temporal`, ...) — a second, differently-
scoped `common` one level down would make `common.X` ambiguous at every import site.

Docstring only: no re-exports, for the same sandbox-import reason every other package
`__init__.py` in this service states its own version of (see `workflows/__init__.py`).
"""
