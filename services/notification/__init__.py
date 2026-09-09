"""SmartFoodOps Notification Service — simulated SMS/email, triggered by Kafka events.

Added in Week 3 (D45). Two deployables share this one image (docker-compose.yml):
`notification-consumer` (`python -m notification.consumer`) reads `sfo.order.events.v1` and
enqueues Celery tasks; `notification-worker` (`celery -A notification.worker worker`) is the
concurrency pool that actually "sends" them. Neither is on any request's critical path —
same as analytics-service, this is a pure side effect of a fact that already happened.

The two-container split is what makes the Celery hop real rather than decorative: the
consumer commits its Kafka offset only *after* the task is enqueued, so Kafka is the
durable ledger and RabbitMQ/Celery is purely the concurrency pool for the slow simulated
I/O — a lost RabbitMQ message is recovered by resetting this consumer group's offset and
replaying, not by RabbitMQ's own persistence.
"""
