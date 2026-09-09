"""The Celery app and its two registered tasks — `celery -A notification.worker worker` is
this file's entrypoint (Week 3, D45).

`task_ignore_result=True` because nothing ever calls `.get()` on a dispatch — a fire-and-
forget SMS/email has no result worth storing, so there is no result backend configured at
all (the blueprint this replaces wired one up on Redis db 2 for exactly nothing).

`task_acks_late=True` + `worker_prefetch_multiplier=1`: a task is only acked after it
finishes, and a worker prefetches one at a time. A worker killed mid-dispatch leaves its
task unacked, so RabbitMQ redelivers it to another worker — the two settings that make an
occasional duplicate SMS the failure mode instead of a silently dropped one. A duplicate
notification is an acceptable, named trade-off here in a way it would not be for a payment:
see this module's own docstring one level up in `notification/__init__.py` for why Kafka,
not this queue, is the ledger that actually matters.
"""

from celery import Celery

from common.config import required, service_url
from notification.tasks import send_customer_email, send_customer_sms

# Credentials are never defaulted in code (common/config.py's own rule, D19) — only the
# credential-free hostname may fall back to the in-network default. An earlier draft of
# this file defaulted the whole broker URL to `amqp://guest:guest@rabbitmq:5672//`, which
# is exactly the hardcoded-password mistake this platform's own blueprint review flagged
# in a different file; caught before it shipped, fixed by requiring the two secrets
# separately and composing the URL from them.
RABBITMQ_HOST = service_url("RABBITMQ_HOST", "rabbitmq")
BROKER_URL = f"amqp://{required('RABBITMQ_USER')}:{required('RABBITMQ_PASSWORD')}@{RABBITMQ_HOST}:5672//"

celery_app = Celery("notification", broker=BROKER_URL)
celery_app.conf.update(
    task_ignore_result=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


@celery_app.task(name="notification.dispatch_sms")
def dispatch_sms(order_id: str, status: str, phone: str, detail: str | None = None) -> None:
    send_customer_sms(order_id, status, phone, detail)


@celery_app.task(name="notification.dispatch_email")
def dispatch_email(order_id: str, status: str, email: str, detail: str | None = None) -> None:
    send_customer_email(order_id, status, email, detail)
