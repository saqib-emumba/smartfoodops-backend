"""Temporal activities, one module per entity this service orchestrates — `order.py`
(`OrderActivities`), and, since D55's child-workflow split, `payment.py`
(`PaymentActivities`) and `rider.py` (`RiderActivities`), each backing the like-named child
workflow.

Splitting the old, unified `OrderActivities` by domain was previously avoided here — this
docstring used to warn that a class split risked renaming a method Temporal had already
recorded in durable workflow history. That risk turned out not to apply: Temporal names an
activity by its **method's own name**, not by `ClassName.method_name` (`OrderActivities`'s
own docstring has always said as much) — `authorize_payment_activity`,
`refund_payment_activity`, `dispatch_rider_activity` and `release_rider_activity` moved to
`PaymentActivities`/`RiderActivities` with their names completely unchanged, so a workflow
already in flight still finds each one by exactly the name it always used. What genuinely
does carry replay risk here is `OrderWorkflow.run()` itself now calling
`execute_child_workflow` where it used to call `execute_activity` directly — a running
workflow replayed against that new code hits a non-determinism error, the same class of risk
D37/D43 already named, closed the same way: draining in-flight workflows before deploying.

A future second orchestrated entity gets its own `activities/<entity>.py` beside these —
never a second class crammed into an existing one.

Docstring only: no re-exports. Each workflow module imports its own activities class
`from orchestrator.activities.<entity> import <Entity>Activities` inside
`imports_passed_through()`, and this package's `__init__.py` is passed through the same way
this comment's own sandbox constraint requires it to stay empty.
"""
