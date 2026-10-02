"""Shared Temporal `RetryPolicy` constants for the order saga's workflows.

Previously each workflow file (`order.py`, `payment.py`, `rider.py`, `compensation.py`)
redefined these under its own name with identical `timedelta` literals — correct, but a future
tuning change (raising `STATE_POLICY`'s `maximum_attempts`, say) had to be applied by hand in every
file, with nothing to catch a missed one. Defined once here instead; each workflow file
imports only the names it uses.

Pure stdlib + `temporalio.common`, so importing this carries none of the
`imports_passed_through()` constraint the service-local imports in each workflow file do (see
`order.py`'s own docstring for that constraint) — it can be imported directly, the same way
`RetryPolicy` and `timedelta` themselves already are in every workflow file.
"""

from datetime import timedelta

from temporalio.common import RetryPolicy

# A round trip to another service — an external call or a best-effort dispatch attempt — is
# seconds, not milliseconds, and worth a few retries but not unlimited ones.
TRANSIENT_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

# A card authorisation is the same kind of round trip as TRANSIENT_POLICY — same bound, under the
# name that reads naturally at its own call site.
AUTHORIZE_POLICY = TRANSIENT_POLICY

# A state write is local to the order database, so it is quick and safe to retry hard.
STATE_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
)

# D53: no `maximum_attempts` — unlimited retries, on purpose. This is the replacement for the
# outbox relay's own "loop forever until Kafka accepts it" contract (`common/outbox.py`,
# deleted): a publish activity's completion in workflow history is the only thing that stops
# Temporal from retrying it, the same durability guarantee a `published_at IS NULL` row used
# to encode in a table instead.
PUBLISH_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
)

# Compensations — refunding money, releasing a claimed rider — retry harder than forward
# progress, and the asymmetry is deliberate: giving up early leaves the system in its worst
# state, a customer charged with no order coming or a rider permanently unavailable. Better to
# keep trying for minutes than to give up.
COMPENSATION_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=10,
)

# A rider release is the same patient policy as COMPENSATION_POLICY, under the name that reads
# naturally at its own call site.
RELEASE_POLICY = COMPENSATION_POLICY
