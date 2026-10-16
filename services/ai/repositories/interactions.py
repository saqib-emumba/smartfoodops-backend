"""The assistant's usage log and the attribution query (Week 5, D63).

Stores identifiers and outcomes only — never a question or an answer — so the log can be kept and
queried without becoming a store of what customers asked an AI.
"""

from uuid import UUID

from common.repository import Repository
from ai import config

_INSERT = """
    INSERT INTO assistant_interactions
        (customer_id, conversation_id, intent, outcome, recommended_restaurant_ids, recommended_item_ids)
    VALUES (%(customer_id)s, %(conversation_id)s, %(intent)s, %(outcome)s,
            %(restaurant_ids)s::uuid[], %(item_ids)s::text[])
"""

# Claims the newest still-unconverted interaction of this customer that named this restaurant
# inside the window. The NOT EXISTS makes a replayed event a no-op: an order is attributed once,
# whichever interaction it lands on. SKIP LOCKED so two consumers never fight over one row.
_ATTRIBUTE = """
    UPDATE assistant_interactions
       SET converted_order_id = %(order_id)s, converted_at = CURRENT_TIMESTAMP
     WHERE id = (
            SELECT id FROM assistant_interactions
             WHERE customer_id = %(customer_id)s
               AND %(restaurant_id)s::uuid = ANY(recommended_restaurant_ids)
               AND converted_order_id IS NULL
               AND created_at >= CURRENT_TIMESTAMP - make_interval(hours => %(window_hours)s)
             ORDER BY created_at DESC
             LIMIT 1
             FOR UPDATE SKIP LOCKED)
       AND NOT EXISTS (SELECT 1 FROM assistant_interactions WHERE converted_order_id = %(order_id)s)
    RETURNING id
"""


class InteractionRepository(Repository):
    def record(
        self,
        *,
        customer_id: UUID | str,
        conversation_id: str,
        intent: str,
        outcome: str,
        restaurant_ids: list[str],
        item_ids: list[str],
    ) -> None:
        with self._db.cursor(commit=True) as cur:
            cur.execute(
                _INSERT,
                {
                    "customer_id": str(customer_id),
                    "conversation_id": conversation_id,
                    "intent": intent,
                    "outcome": outcome,
                    "restaurant_ids": restaurant_ids,
                    "item_ids": item_ids,
                },
            )

    def attribute(self, *, customer_id: str, restaurant_id: str, order_id: str) -> bool:
        """True if this order was attributed to an earlier recommendation."""
        row = self.write_one(
            _ATTRIBUTE,
            {
                "customer_id": customer_id,
                "restaurant_id": restaurant_id,
                "order_id": order_id,
                "window_hours": config.ATTRIBUTION_WINDOW_HOURS,
            },
        )
        return row is not None
