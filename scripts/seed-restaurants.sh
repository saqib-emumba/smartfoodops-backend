#!/usr/bin/env bash
# Add a spread of restaurants and dishes (Chinese, Thai, Japanese, Italian, Arabic, Korean, Indian, Mexican and a
# healthy bowl place), so the assistant has real variety to search: cuisines, soups, noodles, desserts, vegan and
# gluten-free dishes, and restaurants at different distances from the demo kitchen.
#
#   ./scripts/seed-restaurants.sh
#
# Everything goes through the public API, the same as a client: onboard (with a preparation time), then publish
# the menu. Publishing emits menu.published, so the AI Service embeds the dishes by itself within a few seconds.
# Safe to re-run: restaurants already onboarded are reused (ids kept in .demo-state.json) and their menus are
# republished unchanged, which the AI Service skips because the chunk hashes match.
#
# All restaurants belong to demo.owner@smartfoodops.demo (created if missing). Tags come from the fixed
# vocabulary in services/common/dietary.py. Allergen-free tags are only put on dishes where they are plainly true.
# Coordinates are around Islamabad, 1 to 11 km from Demo Kitchen (33.65, 73.09), so distance questions
# ("within 3 km of me") have different answers. Set REST_LAT / REST_LON (the same as for seed-demo.sh) to move
# the whole set to another city; each restaurant is a fixed offset from that point.
#
# Requires: bash, curl, python3. The stack must be up (docker compose up -d).

set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${DEMO_STATE:-$HERE/../.demo-state.json}"
REST_LAT="${REST_LAT:-33.6500}"
REST_LON="${REST_LON:-73.0900}"
DEMO_PASSWORD='DemoPass123!'   # throwaway dev value, not a secret
OWNER_EMAIL="demo.owner@smartfoodops.demo"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
die()  { printf '  %s✗ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

call() {   # call METHOD PATH [JSON] [TOKEN]  ->  CODE, BODY
  local method="$1" path="$2" data="${3:-}" token="${4:-}"
  local args=(-sS -m 60 -X "$method" "$BASE_URL/api/v1$path" -H 'Content-Type: application/json' -w $'\n%{http_code}')
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  [[ -n "$data"  ]] && args+=(-d "$data")
  local out; out=$(curl "${args[@]}" 2>&1) || { CODE=000; BODY="$out"; return; }
  CODE=$(tail -n1 <<<"$out"); BODY=$(sed '$d' <<<"$out")
}
jget() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d$2)" "$1" 2>/dev/null; }
state_get() { python3 -c "import json,sys,os; p=sys.argv[1]; d=json.load(open(p)) if os.path.exists(p) else {}; print(d.get(sys.argv[2],''))" "$STATE" "$1"; }
state_set() { python3 -c "
import json,sys,os
p=sys.argv[1]; d=json.load(open(p)) if os.path.exists(p) else {}
d[sys.argv[2]]=sys.argv[3]; json.dump(d,open(p,'w'),indent=2)" "$STATE" "$1" "$2"; }

login() {
  call POST /users/login "{\"email\":\"$1\",\"password\":\"$2\"}"
  [[ "$CODE" == "200" ]] && jget "$BODY" "['body']['access_token']"
}

step "Owner account"
OWNER_TOKEN=$(login "$OWNER_EMAIL" "$DEMO_PASSWORD")
if [[ -z "$OWNER_TOKEN" ]]; then
  call POST /users/register "{\"email\":\"$OWNER_EMAIL\",\"password\":\"$DEMO_PASSWORD\",\"full_name\":\"Demo Owner\",\"phone\":\"03009990002\",\"role\":\"restaurant_admin\"}"
  [[ "$CODE" == "201" ]] || die "could not register $OWNER_EMAIL ($CODE): $BODY"
  OWNER_TOKEN=$(login "$OWNER_EMAIL" "$DEMO_PASSWORD")
fi
[[ -n "$OWNER_TOKEN" ]] || die "could not sign in as $OWNER_EMAIL"
ok "$OWNER_EMAIL"

DISHES=0; RESTAURANTS=0
# restaurant SLUG "NAME" "ADDRESS" DLAT DLON PREP-MINUTES  (DLAT/DLON: degrees from Demo Kitchen)  (menu JSON on stdin; __RID__ becomes the restaurant id)
restaurant() {
  local slug="$1" name="$2" address="$3" prep="$6" menu rid lat lon
  lat=$(python3 -c "print(round($REST_LAT + $4, 6))"); lon=$(python3 -c "print(round($REST_LON + $5, 6))")
  menu=$(cat)
  rid=$(state_get "rest_$slug")
  if [[ -n "$rid" ]]; then call GET "/restaurants/$rid" "" "$OWNER_TOKEN"; [[ "$CODE" == "200" ]] || rid=""; fi
  if [[ -z "$rid" ]]; then
    call POST /restaurants/onboard "{\"name\":\"$name\",\"address\":\"$address\",\"latitude\":$lat,\"longitude\":$lon,\"capacity\":40,\"prep_time_minutes\":$prep}" "$OWNER_TOKEN"
    [[ "$CODE" == "201" ]] || die "could not onboard $name ($CODE): $BODY"
    rid=$(jget "$BODY" "['body']['id']"); state_set "rest_$slug" "$rid"
  fi
  call POST /menus "${menu//__RID__/$rid}" "$OWNER_TOKEN"
  [[ "$CODE" == "200" ]] || die "could not publish the menu for $name ($CODE): $BODY"
  local n; n=$(grep -o '"item_id"' <<<"$menu" | wc -l | tr -d ' ')
  DISHES=$((DISHES + n)); RESTAURANTS=$((RESTAURANTS + 1))
  ok "$name  ($n dishes, preparation $prep min)  $rid"
}

step "Restaurants and menus"

restaurant golden-dragon "Golden Dragon" "12 Dragon Lane, Islamabad" 0.0043 0.0069 25 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"soups","category_name":"Soups","display_order":1,"items":[
  {"item_id":"hot-sour-soup","name":"Hot and Sour Soup","description":"Peppery, tangy soup with bamboo shoots, mushrooms and tofu","base_price":5.50,"is_available":true,"dietary_tags":["chinese","soup","spicy","hot","vegetarian","dairy-free"]},
  {"item_id":"corn-soup","name":"Chicken Corn Soup","description":"Warm, silky soup with sweet corn, shredded chicken and egg ribbons","base_price":5.00,"is_available":true,"dietary_tags":["chinese","soup","warm","halal","comfort-meal"]},
  {"item_id":"wonton-soup","name":"Prawn Wonton Soup","description":"Delicate prawn wontons in a light ginger broth","base_price":7.00,"is_available":true,"dietary_tags":["chinese","soup","seafood","light-meal","warm"]}]},
 {"category_id":"mains","category_name":"Mains","display_order":2,"items":[
  {"item_id":"chow-mein","name":"Chicken Chow Mein","description":"Stir-fried egg noodles with chicken, cabbage and carrots","base_price":9.00,"is_available":true,"dietary_tags":["chinese","noodles","halal"]},
  {"item_id":"fried-rice","name":"Egg Fried Rice","description":"Wok-fried rice with egg, spring onion and peas","base_price":7.50,"is_available":true,"dietary_tags":["chinese","rice","vegetarian"]},
  {"item_id":"manchurian","name":"Chicken Manchurian","description":"Crispy chicken in a sticky garlic and chilli sauce","base_price":10.50,"is_available":true,"dietary_tags":["chinese","spicy","halal"]},
  {"item_id":"spring-rolls","name":"Vegetable Spring Rolls","description":"Crisp rolls filled with cabbage, carrot and glass noodles","base_price":6.00,"is_available":true,"dietary_tags":["chinese","vegan","dairy-free"]}]}]}
JSON

restaurant bangkok-street "Bangkok Street" "8 Orchid Road, Islamabad" 0.0193 0.0289 20 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"mains","category_name":"Mains","display_order":1,"items":[
  {"item_id":"tom-yum","name":"Tom Yum Prawn Soup","description":"Hot and sour lemongrass soup with prawns, mushrooms and lime","base_price":8.50,"is_available":true,"dietary_tags":["thai","soup","spicy","hot","seafood","gluten-free"]},
  {"item_id":"pad-thai","name":"Chicken Pad Thai","description":"Rice noodles stir-fried with chicken, egg, bean sprouts and tamarind","base_price":10.00,"is_available":true,"dietary_tags":["thai","noodles","halal"]},
  {"item_id":"green-curry","name":"Thai Green Curry","description":"Creamy coconut curry with vegetables and fresh basil, served with jasmine rice","base_price":10.50,"is_available":true,"dietary_tags":["thai","curry","vegan","spicy","gluten-free","dairy-free"]},
  {"item_id":"basil-chicken","name":"Thai Basil Chicken Rice","description":"Minced chicken with holy basil and chilli over steamed rice","base_price":9.50,"is_available":true,"dietary_tags":["thai","rice","spicy","halal"]},
  {"item_id":"mango-rice","name":"Mango Sticky Rice","description":"Sweet coconut sticky rice with ripe mango","base_price":6.00,"is_available":true,"dietary_tags":["thai","dessert","vegan","gluten-free","dairy-free"]}]}]}
JSON

restaurant zen-sushi "Zen Sushi" "5 Harbour View, Islamabad" -0.0187 0.0439 30 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"sushi","category_name":"Sushi","display_order":1,"items":[
  {"item_id":"salmon-nigiri","name":"Salmon Nigiri Set","description":"Eight pieces of fresh salmon nigiri","base_price":14.00,"is_available":true,"dietary_tags":["japanese","sushi","pescatarian","seafood"]},
  {"item_id":"veg-maki","name":"Cucumber Avocado Maki","description":"Twelve rolls of cucumber and avocado with sesame","base_price":9.00,"is_available":true,"dietary_tags":["japanese","sushi","vegan","light-meal"]},
  {"item_id":"combo-platter","name":"Zen Combo Platter","description":"Salmon nigiri, California rolls and tuna maki to share","base_price":22.00,"is_available":true,"dietary_tags":["japanese","sushi","pescatarian","seafood"]}]},
 {"category_id":"hot","category_name":"Hot dishes","display_order":2,"items":[
  {"item_id":"miso-soup","name":"Miso Soup","description":"Light dashi broth with tofu, wakame and spring onion","base_price":3.50,"is_available":true,"dietary_tags":["japanese","soup","light-meal","warm","dairy-free"]},
  {"item_id":"chicken-ramen","name":"Chicken Shoyu Ramen","description":"Wheat noodles in a rich chicken broth with a soft egg","base_price":12.00,"is_available":true,"dietary_tags":["japanese","noodles","soup","hot","halal","comfort-meal"]},
  {"item_id":"tempura","name":"Prawn Tempura (sold out)","description":"Crisp battered prawns with dipping sauce","base_price":11.00,"is_available":false,"dietary_tags":["japanese","seafood"]}]}]}
JSON

restaurant napoli-slice "Napoli Slice" "21 Piazza Street, Islamabad" 0.0093 -0.0211 18 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"mains","category_name":"Mains","display_order":1,"items":[
  {"item_id":"pepperoni","name":"Pepperoni Pizza","description":"Beef pepperoni, mozzarella and tomato sauce on a hand-stretched base","base_price":12.00,"is_available":true,"dietary_tags":["italian","pizza","halal"]},
  {"item_id":"alfredo","name":"Fettuccine Alfredo","description":"Fettuccine in a creamy parmesan sauce with garlic","base_price":11.00,"is_available":true,"dietary_tags":["italian","pasta","vegetarian","comfort-meal"]},
  {"item_id":"arrabbiata","name":"Penne Arrabbiata","description":"Penne in a hot tomato and chilli sauce","base_price":9.50,"is_available":true,"dietary_tags":["italian","pasta","vegan","spicy"]},
  {"item_id":"minestrone","name":"Minestrone Soup","description":"Chunky vegetable and bean soup with herbs","base_price":6.00,"is_available":true,"dietary_tags":["italian","soup","vegan","warm","comfort-meal","dairy-free"]},
  {"item_id":"caesar","name":"Chicken Caesar Salad","description":"Grilled chicken, cos lettuce, parmesan and croutons","base_price":9.00,"is_available":true,"dietary_tags":["italian","salad","halal","light-meal"]},
  {"item_id":"tiramisu","name":"Tiramisu","description":"Coffee-soaked sponge layered with mascarpone cream","base_price":6.50,"is_available":true,"dietary_tags":["italian","dessert","vegetarian"]}]}]}
JSON

restaurant habibi-grill "Habibi Grill" "3 Cedar Avenue, Islamabad" 0.0493 0.0889 25 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"grill","category_name":"Grill and wraps","display_order":1,"items":[
  {"item_id":"shawarma","name":"Chicken Shawarma Wrap","description":"Spiced chicken, garlic sauce, pickles and fries in a warm flatbread","base_price":7.50,"is_available":true,"dietary_tags":["arabic","lebanese","wrap","halal","fast-food"]},
  {"item_id":"mixed-grill","name":"Mixed Grill Platter","description":"Chicken tikka, lamb kofta and seekh kebab with rice and salad","base_price":18.00,"is_available":true,"dietary_tags":["arabic","bbq","kebab","halal","rice"]},
  {"item_id":"lamb-kofta","name":"Lamb Kofta Skewers","description":"Charcoal-grilled minced lamb skewers with grilled tomato","base_price":13.00,"is_available":true,"dietary_tags":["arabic","bbq","kebab","halal","gluten-free"]}]},
 {"category_id":"starters","category_name":"Starters and sweets","display_order":2,"items":[
  {"item_id":"lentil-soup","name":"Lebanese Lentil Soup","description":"Smooth red lentil soup with cumin and lemon","base_price":4.50,"is_available":true,"dietary_tags":["lebanese","soup","vegan","warm","comfort-meal","gluten-free","dairy-free"]},
  {"item_id":"falafel","name":"Hummus and Falafel Plate","description":"Crispy falafel with hummus, tabbouleh and pita","base_price":8.50,"is_available":true,"dietary_tags":["lebanese","vegan","light-meal","dairy-free"]},
  {"item_id":"baklava","name":"Pistachio Baklava","description":"Flaky filo pastry with pistachios and honey syrup","base_price":5.00,"is_available":true,"dietary_tags":["arabic","dessert","vegetarian"]}]}]}
JSON

restaurant seoul-bowl "Seoul Bowl" "17 Han Street, Islamabad" -0.0057 -0.0061 20 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"mains","category_name":"Mains","display_order":1,"items":[
  {"item_id":"bibimbap","name":"Beef Bibimbap","description":"Rice bowl with marinated beef, sauteed vegetables, a fried egg and gochujang","base_price":11.50,"is_available":true,"dietary_tags":["korean","rice","halal","spicy"]},
  {"item_id":"kimchi-stew","name":"Kimchi Tofu Stew","description":"Bubbling spicy stew of kimchi, tofu and vegetables","base_price":9.50,"is_available":true,"dietary_tags":["korean","soup","spicy","hot","vegetarian","comfort-meal"]},
  {"item_id":"fried-chicken","name":"Korean Fried Chicken","description":"Twice-fried chicken glazed in sweet and spicy sauce","base_price":12.50,"is_available":true,"dietary_tags":["korean","halal","spicy","fast-food"]},
  {"item_id":"japchae","name":"Japchae Glass Noodles","description":"Sweet potato noodles stir-fried with vegetables and sesame","base_price":9.00,"is_available":true,"dietary_tags":["korean","noodles","vegan","dairy-free"]}]}]}
JSON

restaurant spice-route "Spice Route" "44 Masala Chowk, Islamabad" -0.0107 0.0289 22 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"mains","category_name":"Mains","display_order":1,"items":[
  {"item_id":"butter-chicken","name":"Butter Chicken","description":"Tandoori chicken in a creamy tomato and butter gravy","base_price":12.00,"is_available":true,"dietary_tags":["indian","curry","halal","comfort-meal"]},
  {"item_id":"palak-paneer","name":"Palak Paneer","description":"Fresh cottage cheese in a spiced spinach gravy","base_price":10.50,"is_available":true,"dietary_tags":["indian","curry","vegetarian","gluten-free"]},
  {"item_id":"dal-tadka","name":"Dal Tadka","description":"Yellow lentils tempered with garlic, cumin and chilli","base_price":8.00,"is_available":true,"dietary_tags":["indian","curry","vegan","comfort-meal","gluten-free","dairy-free"]},
  {"item_id":"lamb-biryani","name":"Lamb Dum Biryani","description":"Slow-cooked basmati rice with tender lamb and saffron","base_price":13.50,"is_available":true,"dietary_tags":["indian","biryani","rice","halal","spicy"]},
  {"item_id":"gulab-jamun","name":"Gulab Jamun","description":"Warm milk dumplings in rose syrup","base_price":4.50,"is_available":true,"dietary_tags":["indian","dessert","vegetarian"]}]}]}
JSON

restaurant taco-loco "Taco Loco" "9 Fiesta Plaza, Islamabad" 0.0343 0.0039 15 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"mains","category_name":"Mains","display_order":1,"items":[
  {"item_id":"chicken-tacos","name":"Grilled Chicken Tacos","description":"Three soft tacos with grilled chicken, salsa and lime","base_price":9.50,"is_available":true,"dietary_tags":["mexican","halal","fast-food"]},
  {"item_id":"bean-burrito","name":"Black Bean Burrito","description":"Large flour tortilla with black beans, rice, corn and guacamole","base_price":8.50,"is_available":true,"dietary_tags":["mexican","vegan","wrap","dairy-free"]},
  {"item_id":"nachos","name":"Loaded Nachos","description":"Tortilla chips with cheese, jalapenos, beans and sour cream","base_price":8.00,"is_available":true,"dietary_tags":["mexican","vegetarian","spicy","fast-food"]},
  {"item_id":"tortilla-soup","name":"Chicken Tortilla Soup","description":"Smoky tomato and chilli broth with chicken and crisp tortilla strips","base_price":7.00,"is_available":true,"dietary_tags":["mexican","soup","spicy","hot","halal"]}]}]}
JSON

restaurant green-bowl "Green Bowl" "2 Garden Road, Islamabad" 0.0023 0.0019 12 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"bowls","category_name":"Bowls, soups and drinks","display_order":1,"items":[
  {"item_id":"buddha-bowl","name":"Roasted Veg Buddha Bowl","description":"Quinoa, roasted sweet potato, chickpeas, kale and tahini","base_price":10.00,"is_available":true,"dietary_tags":["vegan","light-meal","gluten-free","dairy-free","salad"]},
  {"item_id":"keto-salad","name":"Keto Chicken Avocado Salad","description":"Grilled chicken, avocado, egg and leaves with olive oil dressing","base_price":11.00,"is_available":true,"dietary_tags":["keto","low-carb","gluten-free","light-meal","salad","halal"]},
  {"item_id":"pumpkin-soup","name":"Roasted Pumpkin Soup","description":"Smooth pumpkin soup with coconut milk and toasted seeds","base_price":6.00,"is_available":true,"dietary_tags":["soup","vegan","warm","light-meal","gluten-free","dairy-free"]},
  {"item_id":"smoothie","name":"Green Detox Smoothie","description":"Spinach, apple, cucumber, ginger and lime","base_price":5.00,"is_available":true,"dietary_tags":["drink","vegan","light-meal","gluten-free","dairy-free"]}]}]}
JSON

restaurant crispy-corner "Crispy Corner" "6 Zinger Plaza, Islamabad" -0.0043 0.0121 14 <<'JSON'
{"restaurant_id":"__RID__","categories":[
 {"category_id":"burgers","category_name":"Burgers","display_order":1,"items":[
  {"item_id":"zinger","name":"Zinger Burger","description":"Spicy crispy chicken fillet, lettuce and mayo in a soft sesame bun","base_price":6.50,"is_available":true,"dietary_tags":["burger","fast-food","spicy","halal","american"]},
  {"item_id":"double-zinger","name":"Double Zinger Burger","description":"Two spicy crispy chicken fillets, cheese, jalapenos and hot sauce","base_price":9.00,"is_available":true,"dietary_tags":["burger","fast-food","spicy","halal","american"]},
  {"item_id":"crispy-cheese","name":"Crispy Chicken Cheese Burger","description":"Mild crispy chicken fillet with melted cheese and pickles","base_price":7.00,"is_available":true,"dietary_tags":["burger","fast-food","halal","american"]}]},
 {"category_id":"chicken","category_name":"Crispy chicken and wings","display_order":2,"items":[
  {"item_id":"crispy-3pc","name":"Crispy Fried Chicken (3 pieces)","description":"Three pieces of golden, crunchy fried chicken with a dip","base_price":7.50,"is_available":true,"dietary_tags":["fast-food","american","halal"]},
  {"item_id":"crispy-bucket","name":"Crispy Chicken Bucket (8 pieces)","description":"Eight pieces of crunchy fried chicken to share, with fries and two dips","base_price":17.00,"is_available":true,"dietary_tags":["fast-food","american","halal"]},
  {"item_id":"hot-wings","name":"Hot Wings (8 pieces)","description":"Crispy chicken wings tossed in fiery hot sauce","base_price":8.50,"is_available":true,"dietary_tags":["fast-food","american","spicy","hot","halal"]},
  {"item_id":"bbq-wings","name":"Smoky BBQ Wings (8 pieces)","description":"Crispy chicken wings glazed in sweet smoky barbecue sauce","base_price":8.50,"is_available":true,"dietary_tags":["fast-food","american","bbq","halal"]},
  {"item_id":"wings-bucket","name":"Wings Party Bucket (20 pieces)","description":"Twenty wings, half hot and half barbecue, with two dips","base_price":19.00,"is_available":true,"dietary_tags":["fast-food","american","spicy","bbq","halal"]}]},
 {"category_id":"sides","category_name":"Sides and drinks","display_order":3,"items":[
  {"item_id":"fries","name":"Masala Fries","description":"Crinkle-cut fries dusted with spicy masala","base_price":3.00,"is_available":true,"dietary_tags":["fast-food","vegan","spicy"]},
  {"item_id":"shake","name":"Chocolate Milkshake","description":"Thick cold chocolate milkshake","base_price":4.50,"is_available":true,"dietary_tags":["drink","dessert","vegetarian"]}]}]}
JSON

step "Done"
cat <<EOF

  $RESTAURANTS restaurants and $DISHES dishes published. The AI Service embeds them within a few seconds of
  each menu.published event. To embed everything now:  docker compose exec ai-service python -m ai.ingestion --backfill
  Try in the chat UI: "hot soup under \$7", "chinese noodles", "vegan gluten-free dinner", "dessert within 5 km".
EOF
