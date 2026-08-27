"""Outbound calls to sibling services, one module per sibling called.

An order references three things this service's database does not hold: a customer, a
restaurant and a menu. Under database-per-service there is no foreign key that could span
those databases, so each reference is resolved over HTTP instead of by the engine — the
customer against the User Service (user.py), the restaurant against the Restaurant Service
(restaurant.py), and pricing against the Menu Service (menu.py).

The audit trail used to be a fourth call, into the Menu Service's MongoDB. It is not here
any more: `order_tracking_logs` moved into this service's own database, so the first entry
is written in the same transaction as the order rather than posted over the network after
it — see repositories.orders.OrderRepository.create.

An unknown customer or restaurant surfaces as 422 rather than 404, which is what the
foreign-key violation these checks replace already returned: the request is well formed and
the order is not the thing that is missing — the entity it points at is.

user.py, restaurant.py and menu.py all run as the customer who placed the order, by
forwarding their bearer token, so this service can never read more than they could.

orchestrator.py is the exception, on the internal key rather than a forwarded bearer token
— see its own docstring for why, and D26. It replaced payment.py and rider.py here: those
two clients, and the whole Temporal client this service used to hold directly, moved to
`services/orchestrator/` when D36 split the saga's worker into its own deployable that
shares neither this service's image nor its database. What is left in this package is every
call this service's *request path* makes, plus the one call it makes into the process that
now runs the saga on its behalf.

There is no saga client for the Restaurant Service, and that is the whole shape of D32. The
saga used to call it four times — ticket, read-back, expiry and coordinates — and now calls
it zero: the kitchen's decision is a column on `orders`, and the restaurant's capacity and
coordinates ride in the workflow payload, captured once at checkout from a lookup this
service already performed.

Docstring only: no re-exports here. Import from the specific module you need
(`from order.clients.orchestrator import OrchestratorClient`).
"""
