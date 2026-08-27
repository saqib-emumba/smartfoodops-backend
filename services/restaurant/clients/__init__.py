"""Outbound calls to sibling services, one module per sibling called.

Restaurant calls exactly one today — the User Service, to verify an owner — but the
directory shape is kept uniform with the services that call more than one, so `ls
clients/` always answers "who does this service talk to?" the same way everywhere.

Docstring only: no re-exports. Import from the specific module (`from restaurant.clients.user
import UserServiceClient`).
"""
