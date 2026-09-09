"""SmartFoodOps Analytics Service — a Kafka read-model, no lifecycle authority (Port 8008).

Added in Week 3 (D38-D43). Consumes `sfo.order.events.v1`, the same topic order-service's
and payment-service's outbox relays publish to, and derives business-facing counters and a
per-order projection from it — nothing here ever writes back to `orders` or `payments`, and
nothing here is on any request's critical path: this service could be stopped for a day and
every other service in the platform would keep working exactly as before, because it reads
facts nobody is waiting on.

A package rather than loose modules so its own modules import absolutely
(`from analytics.repositories.projections import ...`). The Dockerfile copies it to
/app/analytics/, the same convention every other service in the platform already follows.
"""
