"""Deterministic checks of the assistant's guardrails and router, run inside the AI container by smoke-test.sh:

    docker exec -i sfo-ai-service python - < scripts/ai-guardrail-checks.py

Prints one JSON object of {check name: passed}. No model, no network, no database: these are the rules that decide
whether an answer is trusted, tested against answers written to break them. A real model rarely misbehaves on
demand, so this is where each rule is shown to fire. The order-history facts below are a made-up customer.
"""
import json
from ai.llm.guardrails import verify_counts, verify_quantities, verify_food_answer
from ai.assistant.order_explainer import render_history, check_history, history_counts
from ai.assistant.agents.router import parse_router
from common.delivery import delivery_estimate, minutes_remaining
from datetime import datetime, timedelta, timezone
from ai.assistant.order_explainer import OrderExplainer, check_explanation, quantities_in

facts = {"kind": "order_history", "total_orders": 7, "in_progress": 2, "delivered": 4, "cancelled": 1,
         "recent_orders_shown": 2, "unavailable": [],
         "recent_orders": [
           {"restaurant": "Demo Kitchen", "status": "in progress (with the restaurant)", "total": "$20.50",
            "items": ["1 x Chicken Karahi", "2 x Sweet Mango Lassi"], "placed": "about 10 minutes ago", "placed_minutes_ago": 10},
           {"restaurant": "Demo Kitchen", "status": "delivered", "total": "$11.00",
            "items": ["1 x Beef Biryani"], "placed": "on 18 October 2026", "placed_minutes_ago": None}]}
facts["plain_summary"] = render_history(facts)
counts = history_counts(facts)
out = {}
out["fallback_passes_own_check"] = check_history(facts["plain_summary"], facts).ok
out["true_counts_pass"] = verify_counts("You have 2 orders in progress, 4 delivered and 1 cancelled out of 7 orders.", counts=counts).ok
out["word_counts_pass"] = verify_counts("You have two pending orders.", counts=counts).ok
out["wrong_count_digit_fails"] = not verify_counts("You have 3 pending orders.", counts=counts).ok
out["wrong_count_word_fails"] = not verify_counts("You have five delivered orders.", counts=counts).ok
out["wrong_status_count_fails"] = not verify_counts("3 delivered so far.", counts=counts).ok
out["item_quantity_is_not_a_count"] = verify_counts("Your last order was 2 x Sweet Mango Lassi.", counts=counts).ok
out["invented_price_fails"] = not check_history("Your last order cost $99.00.", facts).ok
out["real_price_passes"] = check_history("Your last order cost $20.50.", facts).ok
out["invented_minutes_fail"] = not check_history("It was placed about 45 minutes ago.", facts).ok
out["verdict_fails"] = not check_history("Your order is running late.", facts).ok
out["courier_minutes_quoted_pass"] = verify_quantities("A courier could reach it in about 13 minutes.", quantities={13.0}).ok
out["invented_courier_minutes_fail"] = not verify_quantities("A courier could reach it in about 7 minutes.", quantities={13.0}).ok
out["food_answer_unknown_minutes_fail"] = not verify_food_answer("**A** is $5.00, ready in 20 minutes.", names={"a"}, prices={5.0}, quantities={13.0}).ok
out["food_answer_asked_minutes_ok"] = verify_food_answer("**A** is $5.00.", names={"a"}, prices={5.0}, quantities={13.0}).ok
out["a_bolded_price_is_not_a_dish_name"] = verify_food_answer("**A** is **$5.00**.", names={"a"}, prices={5.0}).ok
out["a_bolded_wrong_price_still_fails"] = not verify_food_answer("**A** is **$6.00**.", names={"a"}, prices={5.0}).ok
out["a_bolded_time_window_is_not_a_dish_name"] = verify_food_answer("**A** takes **20 to 30 minutes**.", names={"a"}, prices=set(), quantities={20.0, 30.0}).ok
out["a_bolded_wrong_time_still_fails"] = not verify_food_answer("**A** takes **20 to 99 minutes**.", names={"a"}, prices=set(), quantities={20.0, 30.0}).ok
out["a_bolded_invented_dish_still_fails"] = not verify_food_answer("**Zzz Special** is $5.00.", names={"a"}, prices={5.0}).ok
out["router_knows_order_history"] = parse_router('{"intent":"order_history"}').intent == "order_history"
out["router_still_rejects_unknown"] = parse_router('{"intent":"mystery"}').intent == "food_discovery"
# --- the delivery-time estimate (common/delivery.py, D68) ---------------------------------------------------
est = delivery_estimate(1.0, 3.0)                      # 15 prep + 2 courier + 6 trip = 23 -> centre 25
out["estimate_is_a_ten_minute_window"] = (est["minutes_low"], est["minutes_high"], est["trip_assumed"]) == (20, 30, False)
out["unknown_trip_is_assumed_and_flagged"] = delivery_estimate(None, None)["trip_assumed"] is True
out["farther_means_longer"] = delivery_estimate(1.0, 10.0)["minutes_high"] > delivery_estimate(1.0, 3.0)["minutes_high"]
out["beyond_range_has_no_window"] = delivery_estimate(1.0, 20.0) == {"out_of_range": True}
out["time_already_spent_is_taken_off"] = minutes_remaining(est, 6) == {"minutes_low": 14, "minutes_high": 24, "prep_minutes": 15, "trip_assumed": False}
out["remaining_never_goes_negative"] = minutes_remaining(est, 90) == {"minutes_low": 0, "minutes_high": 5, "prep_minutes": 15, "trip_assumed": False}
slow = delivery_estimate(1.0, 3.0, prep_minutes=25)     # the restaurant's own, longer, preparation time
out["a_restaurants_own_prep_time_is_used"] = (slow["minutes_low"], slow["minutes_high"], slow["prep_minutes"]) == (30, 40, 25)
out["a_slower_kitchen_means_a_longer_estimate"] = slow["minutes_high"] > est["minutes_high"]
out["no_prep_time_falls_back_to_the_default"] = delivery_estimate(1.0, 3.0, None)["prep_minutes"] == 15

now = datetime.now(timezone.utc)
def at(m): return (now - timedelta(minutes=m)).isoformat()
context = {"order_id": "o", "status": "assigned", "kitchen_decision": "accepted", "kitchen_decided_at": at(5),
           "rider_assigned": True, "saga": {"fulfillment_stage": "dispatching_rider"},
           "timeline": [{"status": "created", "occurred_at": at(6)}, {"status": "confirmed", "occurred_at": at(6)},
                        {"status": "assigned", "occurred_at": at(4), "estimated_courier_minutes": 5,
                         "courier_distance_at_assignment_km": 1.0}]}
kitchen = {"name": "Demo Kitchen", "latitude": 24.8607, "longitude": 67.0011, "prep_time_minutes": 20}
order = OrderExplainer.__new__(OrderExplainer)._build(context, kitchen, None, [])
window = order["delivery_estimate"]       # 20 prep + 2 courier + 6 assumed trip = 28 -> 25-35, 6 minutes already spent
out["order_facts_carry_a_window"] = (window["minutes_low"], window["minutes_high"], window["prep_minutes"]) == (19, 29, 20)
out["estimate_figures_are_allowed"] = {19.0, 29.0, 20.0} <= quantities_in(order)
out["summary_states_the_window"] = "19 to 29 more minutes" in order["plain_summary"]
out["summary_passes_its_own_check"] = check_explanation(order["plain_summary"], order).ok
out["the_real_window_passes"] = check_explanation("About 19 to 29 more minutes, an estimate.", order).ok
out["the_preparation_time_may_be_quoted"] = check_explanation("It includes 20 minutes of preparation.", order).ok
out["an_invented_window_fails"] = not check_explanation("About 3 to 7 more minutes.", order).ok
out["an_invented_low_end_alone_fails"] = not check_explanation("About 3 to 29 more minutes.", order).ok
out["an_invented_high_end_alone_fails"] = not check_explanation("About 19 to 40 minutes.", order).ok
out["a_dashed_range_is_read_too"] = not check_explanation("About 3-29 minutes.", order).ok
far = OrderExplainer.__new__(OrderExplainer)._build(context, kitchen, None, [], (33.68, 73.04))
near = OrderExplainer.__new__(OrderExplainer)._build(context, kitchen, None, [], (24.88, 67.03))     # about 3 km away
nowhere = OrderExplainer.__new__(OrderExplainer)._build(context, kitchen, None, [])
out["a_placed_order_is_never_out_of_range"] = "out_of_range" not in far["delivery_estimate"] and "outside" not in far["plain_summary"]
out["a_location_beyond_range_is_ignored_for_a_placed_order"] = far["delivery_estimate"] == nowhere["delivery_estimate"] and far["delivery_estimate"]["trip_assumed"] is True
out["a_nearby_location_is_used_for_a_placed_order"] = near["delivery_estimate"]["trip_assumed"] is False
out["the_far_order_summary_passes_its_own_check"] = check_explanation(far["plain_summary"], far).ok
print(json.dumps(out))
