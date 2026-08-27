"""Routers for the Rider Service, one module per audience — plus `health`, on its own.

`profile` and `delivery` are rider-facing on a bearer token; `dispatch` is saga-facing on
the internal key. The split follows that boundary rather than the URL shape, because who
may call a route is the thing worth seeing at a glance here.

Docstring only: no re-exports, so main.py names the module it wants.
"""
