"""Raw SQL for `orders` and `order_tracking_logs`, and the one seam both tables share.

`_append_log` is the reason this module exists rather than letting each repository hold its
own strings: OrderRepository.create and OrderRepository.transition write the trail inside
the same transaction as the order, and OrderTrackingRepository.append writes it for a
sibling's report — three call sites, one INSERT, one place that can drift.
"""

_COLUMNS = (
    "id, customer_id, restaurant_id, rider_id, total_amount, status, "
    "kitchen_decision, rider_reported_stage, idempotency_key"
)

# What the kitchen is shown, and deliberately less than _COLUMNS. An admin deciding on an
# order needs to know what to cook; they have no business seeing what the customer paid or
# which idempotency key their client chose.
_KITCHEN_COLUMNS = "id, restaurant_id, status, created_at"

# `items` is no longer a column on either row above (D50) — OrderRepository attaches it
# separately, from order_line_items/order_line_item_options, after these queries run.

# "On the rail" — confirmed, and the kitchen has not answered yet. This one predicate is
# both the capacity count and the admin's queue, which is why the partial index in
# db/order/init.sql matches it exactly.
ON_RAIL = "status = 'confirmed' AND kitchen_decision IS NULL"

SELECT_BY_ID = f"SELECT {_COLUMNS} FROM orders WHERE id = %s"

SELECT_BY_KEY = f"SELECT {_COLUMNS} FROM orders WHERE idempotency_key = %s"

INSERT_ORDER = f"""
    INSERT INTO orders (customer_id, restaurant_id, total_amount, status, idempotency_key)
    VALUES (%s, %s, %s, 'created', %s)
    RETURNING {_COLUMNS}
"""

# One row per submitted line item, written once at checkout and never updated again — the
# same write-once guarantee the JSONB column it replaced (D50) offered.
INSERT_LINE_ITEM = """
    INSERT INTO order_line_items
        (order_id, line_no, menu_item_id, item_name, quantity, unit_price, line_total, customizations)
    VALUES (%(order_id)s, %(line_no)s, %(menu_item_id)s, %(item_name)s,
            %(quantity)s, %(unit_price)s, %(line_total)s, %(customizations)s)
    RETURNING id
"""

INSERT_LINE_ITEM_OPTION = """
    INSERT INTO order_line_item_options (line_item_id, group_key, name, extra_price, position)
    VALUES (%(line_item_id)s, %(group_key)s, %(name)s, %(extra_price)s, %(position)s)
"""

# Batched rather than one-row-at-a-time: kitchen_queue attaches items to many orders at
# once, and a single find() is just the N=1 case of the same query.
SELECT_LINE_ITEMS_FOR_ORDERS = """
    SELECT id, order_id, line_no, menu_item_id, item_name, quantity, unit_price, line_total, customizations
      FROM order_line_items
     WHERE order_id = ANY(%(order_ids)s::uuid[])
     ORDER BY order_id, line_no
"""

SELECT_LINE_ITEM_OPTIONS_FOR_LINE_ITEMS = """
    SELECT line_item_id, group_key, name, extra_price
      FROM order_line_item_options
     WHERE line_item_id = ANY(%(line_item_ids)s::uuid[])
     ORDER BY line_item_id, position
"""

# `old_status` is never accepted from a caller: it is read from the preceding entry so the
# chain cannot disagree with itself. The columns are aliased to the names the API has always
# used, which is what kept the migration off MongoDB invisible to clients.
_LOG_COLUMNS = (
    "id, order_id, old_status AS previous_status, new_status AS status, "
    "service, updated_by, raw_log, metadata, created_at"
)

_INSERT_LOG = f"""
    INSERT INTO order_tracking_logs
        (order_id, old_status, new_status, service, updated_by, raw_log, metadata)
    SELECT %(order_id)s::uuid,
           (SELECT prior.new_status
              FROM order_tracking_logs AS prior
             WHERE prior.order_id = %(order_id)s::uuid
             ORDER BY prior.seq DESC
             LIMIT 1),
           %(new_status)s::order_status,
           %(service)s,
           %(updated_by)s,
           %(raw_log)s,
           %(metadata)s::jsonb
    RETURNING {_LOG_COLUMNS}
"""

SELECT_TIMELINE = f"""
    SELECT {_LOG_COLUMNS} FROM order_tracking_logs WHERE order_id = %s ORDER BY seq
"""

# Counts the rail this order is joining, deriving the restaurant from the order itself so
# the caller does not have to supply it — and so the count and the transition it gates stay
# one statement apart inside one transaction.
COUNT_ON_RAIL_FOR_ORDER = f"""
    SELECT count(*) AS on_rail FROM orders
     WHERE restaurant_id = (SELECT restaurant_id FROM orders WHERE id = %(order_id)s::uuid)
       AND {ON_RAIL}
"""

SELECT_KITCHEN_QUEUE = f"""
    SELECT {_KITCHEN_COLUMNS} FROM orders
     WHERE restaurant_id = %s AND {ON_RAIL}
     ORDER BY created_at
"""

# Guarded on both halves of "undecided": a second accept, or an accept racing a reject,
# changes nothing and the caller reports whichever decision actually stuck. The status
# clause also refuses a decision on an order that has already been cancelled out from
# under the kitchen — which is what a late click on a timed-out order is.
DECIDE_KITCHEN = f"""
    UPDATE orders
       SET kitchen_decision = %(decision)s::kitchen_decision,
           kitchen_decided_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = %(order_id)s::uuid
       AND {ON_RAIL}
    RETURNING {_COLUMNS}
"""

# Guarded forward-only, the same argument D31 makes for TRANSITION_ORDER below: a rider
# retrying a pickup call after delivery has already been reported must not walk the column
# back to 'picked_up'. `rider_report_stage` was declared in lifecycle order for exactly
# this comparison (Week 3, D46).
RECORD_RIDER_REPORT = f"""
    UPDATE orders
       SET rider_reported_stage = %(stage)s::rider_report_stage,
           rider_reported_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = %(order_id)s::uuid
       AND (rider_reported_stage IS NULL
            OR %(stage)s::rider_report_stage > rider_reported_stage)
    RETURNING {_COLUMNS}
"""

# A compare-and-set, not a blind UPDATE, and every clause earns its place.
#
# Temporal guarantees activities run *at least* once, so this statement is executed more
# than once for a single logical transition whenever a worker dies mid-activity or a
# response is lost. Three things follow from that:
#
#   `status <> new` makes a replay a no-op instead of a second identical transition, which
#   is what keeps the audit trail from growing an entry per retry.
#
#   `status NOT IN ('delivered','cancelled')` makes the terminal states final. A late
#   signal for an order that has already been cancelled cannot resurrect it.
#
#   The last clause only allows forward movement, except into 'cancelled', which is
#   reachable from anywhere still in flight. It leans on a property of the schema worth
#   knowing: a Postgres enum compares by *declaration order*, and `order_status` was
#   declared in lifecycle order, so `'delivered' > 'assigned'` is simply true. That is why
#   no separate ordering table is needed here.
#
# `rider_id` is COALESCEd so a later transition never clears an assignment made earlier.
TRANSITION_ORDER = f"""
    UPDATE orders
       SET status = %(new_status)s::order_status,
           rider_id = COALESCE(%(rider_id)s::uuid, rider_id),
           updated_at = CURRENT_TIMESTAMP
     WHERE id = %(order_id)s::uuid
       AND status <> %(new_status)s::order_status
       AND status NOT IN ('delivered', 'cancelled')
       AND (
             %(new_status)s::order_status = 'cancelled'
             OR %(new_status)s::order_status > status
           )
    RETURNING {_COLUMNS}
"""


def append_log(cur, entry: dict) -> dict:
    """Append one transition on an already-leased cursor, and return the row written.

    Takes a cursor rather than the pool so the caller decides the transaction: creating an
    order writes its first entry inside the same one, while a later transition arrives on
    its own.
    """
    cur.execute(_INSERT_LOG, entry)
    return cur.fetchone()
