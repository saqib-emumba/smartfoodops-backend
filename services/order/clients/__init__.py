"""Outbound calls to sibling services, one module per sibling called.

An order references three things this service's database does not hold: a customer, a
restaurant and a menu. Under database-per-service there is no foreign key that could span
those databases, so each reference is resolved over HTTP instead of by the engine — the
customer against the User Service (user.py), the restaurant against the Restaurant Service
(restaurant.py), and pricing against the Menu Service (menu.py).

The audit trail used to be a fourth call, into the Menu Service's MongoDB. It is not here
any more: `order_tracking_logs` moved into this service's own database, so the first entry
is written in the same transaction as the order rather than posted over the network after
it — see repository.OrderRepository.create.

An unknown customer or restaurant surfaces as 422 rather than 404, which is what the
foreign-key violation these checks replace already returned: the request is well formed and
the order is not the thing that is missing — the entity it points at is.

Every *request-path* lookup (user.py, restaurant.py, menu.py) runs as the customer who
placed the order, by forwarding their bearer token, so this service can never read more
than they could.

payment.py and rider.py are the exception, and the exception is the point. An activity has
no user behind it: a bearer token would be written into durable, UI-visible workflow
history and would expire long before a saga that waits on a kitchen finishes. Those calls
carry the internal key instead (D26). They are used only by activities.py, on the internal
key, and each returns the sibling's raw response so the activity — not the transport —
decides what a business outcome means.

There is no saga client for the Restaurant Service, and that is the whole shape of D32. The
saga used to call it four times — ticket, read-back, expiry and coordinates — and now calls
it zero: the kitchen's decision is a column on `orders`, and the restaurant's capacity and
coordinates ride in the workflow payload, captured once at checkout from a lookup this
service already performed.

Splitting this file per callee is not cosmetic: activities.py imports only payment.py and
rider.py, so the worker process no longer executes user.py/restaurant.py/menu.py at all —
and with them, no longer resolves RESTAURANT_SERVICE_URL or MENU_SERVICE_URL, which it was
never given in docker-compose.yml. That absence is now structural, not just true in code.

Docstring only: no re-exports here. Import from the specific module you need
(`from order.clients.payment import SagaPaymentClient`) — a re-export would put every
client, request-path and saga alike, back in the worker's import graph.
"""
