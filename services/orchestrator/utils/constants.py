"""Small shared literals used by more than one workflow in this saga.

Pure constants, no service imports — importable directly, same as `policies.py`.
"""

# Truncation length for free-text `detail` strings stored in order-transition metadata — long
# enough to keep a useful error message, short enough to not bloat the metadata column.
DETAIL_TRUNCATE_LENGTH = 500

# `PaymentWorkflow.run`'s `payload["mode"]` values. Named here so the saga caller (`order.py`)
# and the manual caller (`services/payment/apis/payments.py`) share one symbol instead of two
# independently-typed string literals that merely have to happen to match.
PAYMENT_MODE_MANUAL = "manual"
PAYMENT_MODE_SAGA = "saga"
