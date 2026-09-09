#!/usr/bin/env bash
# Creates the platform's Kafka topics explicitly, at the partition count per-order
# ordering depends on, then exits — this is a migration step, not a long-running service
# (see kafka-init's `restart: "no"` in docker-compose.yml).
#
# `--if-not-exists` makes every run idempotent, so `docker compose up` can run this
# against an already-initialised broker with no effect. Partition count and topic names
# must match services/common/events/topics.py — that Python module is the source of truth
# for what a producer/consumer expects to exist; this script is what makes it exist.
#
# Auto-create is deliberately disabled on the broker (KAFKA_AUTO_CREATE_TOPICS_ENABLE:
# 'false'): without an explicit init step, Kafka would silently create a 1-partition topic
# on the first publish, and a typo'd topic name would create a new topic instead of failing.

set -euo pipefail

BOOTSTRAP_SERVER="kafka:29092"
PARTITIONS=3
REPLICATION_FACTOR=1

create_topic() {
  local topic="$1"
  kafka-topics --bootstrap-server "$BOOTSTRAP_SERVER" --create --if-not-exists \
    --topic "$topic" --partitions "$PARTITIONS" --replication-factor "$REPLICATION_FACTOR"
}

create_topic "sfo.order.events.v1"
create_topic "sfo.order.events.v1.dlq"

echo "topics ready"
