"""Topic and DLQ names. One topic for the whole order aggregate (D40) — order and payment
events both ride it, keyed by `order_id`, because Kafka only orders records within one
topic-partition and every consumer here depends on seeing one order's events in order.
"""

ORDER_EVENTS_TOPIC = "sfo.order.events.v1"
ORDER_EVENTS_DLQ_TOPIC = "sfo.order.events.v1.dlq"

# 3 partitions: enough for a consumer group of up to 3 without ever breaking per-order
# ordering (replication 1 is the ceiling on a single-broker dev cluster).
PARTITION_COUNT = 3
REPLICATION_FACTOR = 1
