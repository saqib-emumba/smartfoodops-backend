"""Simulated SMS and email dispatch — the seam this service exists around.

The one module that pretends to talk to a real gateway, the same shape D10 already
established for `services/payment/gateway.py`: everything else in this service (the
consumer, the Celery task wiring) is infrastructure that would be identical if this file
called a real SMS/email provider tomorrow instead of `time.sleep`.
"""

import logging
import time

logger = logging.getLogger("notification.tasks")


def send_customer_sms(order_id: str, status: str, phone: str, detail: str | None = None) -> None:
    logger.info("[SMS DISPATCH START] order=%s status=%s -> %s", order_id, status, phone)
    time.sleep(1.5)  # simulated gateway latency
    suffix = f" ({detail})" if detail else ""
    logger.info(
        "[SMS DISPATCH SUCCESS] order=%s status=%s%s sent to %s", order_id, status, suffix, phone
    )


def send_customer_email(order_id: str, status: str, email: str, detail: str | None = None) -> None:
    logger.info("[EMAIL DISPATCH START] order=%s status=%s -> %s", order_id, status, email)
    time.sleep(2.0)  # simulated gateway latency
    suffix = f" ({detail})" if detail else ""
    logger.info(
        "[EMAIL DISPATCH SUCCESS] order=%s status=%s%s sent to %s", order_id, status, suffix, email
    )
