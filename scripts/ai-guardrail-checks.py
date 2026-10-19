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
from ai.assistant.agents import graph as assistant_graph
from ai.assistant.agents.context import no_results_message
from ai.llm.guardrails import verify_no_internal_terms
from ai.retrieval.retriever import _within
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
# --- scenarios from the Week 4/5 spec and what customers actually type (D69) ---------------------------------------
decision = parse_router(json.dumps({"intent": "food_discovery", "search_query": "  light   meal ", "restaurant_name": "Zen Sushi", "max_distance_km": 3}))
out["router_reads_a_cleaned_search_phrase"] = decision.search_query == "light meal"
out["router_reads_a_named_restaurant"] = decision.restaurant_name == "Zen Sushi"
out["router_reads_a_distance_limit"] = decision.max_distance_km == 3.0
out["router_rejects_an_absurd_distance"] = parse_router('{"intent":"food_discovery","max_distance_km":0}').max_distance_km is None and parse_router('{"intent":"food_discovery","max_distance_km":5000}').max_distance_km is None
out["router_ignores_non_text_names"] = parse_router('{"intent":"food_discovery","restaurant_name":42,"search_query":["x"]}').restaurant_name is None
out["router_bounds_a_long_name"] = len(parse_router(json.dumps({"intent": "food_discovery", "restaurant_name": "x" * 500})).restaurant_name) == 80
order_ref = assistant_graph._ORDER_NUMBER
out["order_hash_number_is_recognised"] = bool(order_ref.search("Explain the current delivery status of Order #102."))
out["order_number_words_are_recognised"] = bool(order_ref.search("where is order number 42")) and bool(order_ref.search("order no. 7"))
out["an_ordinary_order_question_is_not_a_number"] = not order_ref.search("Where is my order from Demo Kitchen? It was placed 5 minutes ago") and not order_ref.search("my order")
personal = assistant_graph._PERSONAL
out["history_based_asks_are_recognised"] = all(personal.search(q) for q in ["Suggest something based on what I ordered before", "what I had last time again", "my usual please", "something similar to what I ordered", "reorder my last meal"])
out["plain_food_asks_are_not_personal"] = not personal.search("Recommend a spicy dish under $15") and not personal.search("something light for a warm afternoon")
out["prompt_labels_may_not_reach_a_customer"] = not verify_no_internal_terms("CONTEXT has no address for it.").ok and not verify_no_internal_terms("Per FACTS the order is late.").ok
out["field_names_may_not_reach_a_customer"] = not verify_no_internal_terms("Your total_orders is 3 and trip_assumed is true.").ok
out["ordinary_words_are_fine"] = verify_no_internal_terms("The context of your order is simple: the facts are clear.").ok
out["leaks_are_caught_in_food_answers"] = not verify_food_answer("**A** is $5.00. CONTEXT has no more.", names={"a"}, prices={5.0}).ok
out["leaks_are_caught_in_order_answers"] = not check_explanation("According to FACTS, about 19 to 29 more minutes.", order).ok
out["leaks_are_caught_in_history_answers"] = not check_history("Your recent_orders show 7 orders.", facts).ok
out["no_results_names_the_allergy_filter"] = "tagged nut-free" in no_results_message(["nut-free"], None, None)
out["no_results_names_every_limit"] = all(x in no_results_message(["vegan"], 5.0, 3.0) for x in ("tagged vegan", "under $5.00", "within 3 km"))
out["no_results_without_limits_is_the_plain_message"] = no_results_message([], None, None).startswith("I couldn't find anything on the current menus")
here = (24.8607, 67.0011, 3.0)
out["a_nearby_restaurant_is_within_range"] = _within({"latitude": 24.87, "longitude": 67.01}, here)
out["a_far_restaurant_is_not"] = not _within({"latitude": 33.68, "longitude": 73.04}, here)
out["no_limit_means_everywhere"] = _within({"latitude": 33.68, "longitude": 73.04}, None)
out["an_unlocated_restaurant_cannot_be_shown_within_a_limit"] = not _within({"latitude": None, "longitude": None}, here)
print(json.dumps(out))
