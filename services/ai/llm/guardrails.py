"""The grounding check: what the model said, held against what it was given (Week 5, D63).

The prompt asks the model to stay inside the context. This module is why that is not the only
thing standing between a customer and an invented dish: after generation, the answer is checked in
plain code, and an answer that names something the context did not contain is replaced by a
deterministic one built from the context itself.

Dish and restaurant names are checked because the generation prompt makes the model write every
one in **bold**, exactly as given — so the set of names an answer asserts is the set of bold spans,
and no language understanding is needed to extract it. Prices are checked because `$` followed by
digits is unambiguous. Both checks are deliberately strict: a false alarm costs a plainer answer; a
miss costs a customer an item that does not exist.

It also checks that a dish, its restaurant and its price belong together within a sentence (`verify_pairings`).

What it does not catch: a false claim made without a bold name, a price or a quantity (an
invented ingredient in prose, say). The prompt is the only guard there, which is why it also
forbids it, and why the ceiling is stated rather than hidden.
"""

import re
from dataclasses import dataclass, field

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
# A price is `$12.50` — or, because a model asked to "quote prices exactly" may drop the symbol (Claude did:
# "priced at 12.50"), a bare number with exactly two decimals that is not a distance or a duration. Without the
# second form an answer with no `$` in it was never price-checked at all.
_PRICE = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)|(?<![\d.$])(\d+\.\d{2})(?!\d)(?!\.\d)(?!\s*(?:km|kilomet|minute|min\b|[ap]\.?m\b))", re.I)


def _prices_in(text: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in _PRICE.finditer(text)]
# "12 minutes", "12 more minutes", "4.3 km" — and a range, "20 to 30 minutes" / "20-30 minutes", whose *both* ends are
# figures (reading only the number beside the unit would let an invented low end through).
_UNIT = r"(?:more\s+|extra\s+|additional\s+)?(?:minutes?|mins?|km)\b"
_QUANTITY = re.compile(rf"(\d+(?:\.\d+)?)\s*{_UNIT}", re.I)
_QUANTITY_RANGE = re.compile(rf"(\d+(?:\.\d+)?)\s*(?:to|-|–|—)\s*(\d+(?:\.\d+)?)\s*{_UNIT}", re.I)


def quantities_stated(text: str) -> list[str]:
    """Every figure in `text` that sits before a time or distance unit, both ends of a range included."""
    figures = list(_QUANTITY.findall(text))
    for low, high in _QUANTITY_RANGE.findall(text):
        figures.append(low)  # the high end was already read by `_QUANTITY`
    return figures
_TRAILING = re.compile(r"[\s\.,;:!?'’]+$|['’]s$")


def normalize(name: str) -> str:
    """Case, spacing and trailing punctuation ('s, a full stop) ignored; nothing else is."""
    collapsed = " ".join(name.lower().split())
    return _TRAILING.sub("", collapsed)


@dataclass
class Verdict:
    ok: bool = True
    # (kind, offending text) — `kind` is the metric label.
    violations: list[tuple[str, str]] = field(default_factory=list)

    def add(self, kind: str, detail: str) -> None:
        self.ok = False
        self.violations.append((kind, detail))


def _is_figure(span: str) -> bool:
    """A bold span that is only a price or a time ("$10.00", "20 to 30 minutes"): no letters once the units go.

    The prompt asks for bold on names and nothing else, but a model sometimes bolds a figure too. That is not a
    claim that a dish by that name exists, and the price and quantity checks already hold the figure itself to the
    context, so judging it as a name would only reject a good answer for its typography.
    """
    return not re.search(r"[a-z]", re.sub(r"\b(?:minutes?|mins?|km|to|more)\b", "", span.lower()))


def name_families(names: set[str]) -> set[str]:
    """Word-prefixes shared by at least two known names: "desi dhaba" for "desi dhaba 0790" and "desi dhaba 1409".

    Claude, summarising three branches of one restaurant, wrote **Desi Dhaba** — what a person would say, and
    not a name in the context, so the strict name check rejected a good answer and replaced it with a worse one.
    A stem shared by two or more real names is a family, not an invention. A prefix of only one name is not
    (a made-up "Smoky BBQ" is still rejected when only "Smoky BBQ Burger" exists).
    """
    counts: dict[str, int] = {}
    for name in names:
        words = name.split()
        for k in range(1, len(words)):
            prefix = " ".join(words[:k])
            counts[prefix] = counts.get(prefix, 0) + 1
    return {prefix for prefix, n in counts.items() if n >= 2}


def bold_names(answer: str) -> list[str]:
    return [normalize(span) for span in _BOLD.findall(answer)]


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
# A sentence that denies a pairing ("X does not have Y") asserts nothing to check, and replacing a correct
# denial with a list would answer a different question. Names are still checked; only pairings are skipped.
_NEGATION = re.compile(r"\b(?:not|no|never|neither|nor|without|cannot)\b|n['’]t\b", re.I)


def verify_pairings(answer: str, *, offers: list[tuple[str, str, float]], budget: float | None = None) -> Verdict:
    """Names and prices that each exist somewhere must also *belong together*.

    The set checks in `verify_food_answer` pass a sentence like "**Margherita Pizza** at **Ollama Test
    Grill** for $9.50" when the context holds a Margherita Pizza (at another restaurant, for $12) and the
    Grill's $9.50 burger: every token is real and the claim is false. Found by running the assistant on a
    real local model, which made exactly that mistake. So within each sentence:

      - a dish must be offered by at least one of the restaurants named in the same sentence;
      - a price must be a price of at least one dish named in the sentence, at the restaurant named there
        if there is one (or the customer's own budget).

    `offers` is `(dish, restaurant, price)` for every dish in the retrieved context, names normalised.
    Deliberately lenient where a sentence names several dishes or restaurants: it asks that each claim be
    *possible*, not that a particular pairing be read out of free text. A negated sentence is skipped (see
    `_NEGATION`) — a known soft spot: "X is not only at Y" would slip through, which is rarer than a correct
    "X does not have Y" being thrown away.
    """
    dishes_at: dict[str, list[tuple[str, float]]] = {}
    for dish, restaurant, price in offers:
        dishes_at.setdefault(dish, []).append((restaurant, price))
    restaurants = {restaurant for _, restaurant, _ in offers}

    # Keep a dot inside a bold name ("St. Louis Pizza") from being read as the end of a sentence.
    protected = _BOLD.sub(lambda m: m.group(0).replace(".", "\x01"), answer)
    verdict = Verdict()
    for sentence in _SENTENCE.split(protected):
        sentence = sentence.replace("\x01", ".")
        if _NEGATION.search(sentence):
            continue
        spans = [normalize(s) for s in _BOLD.findall(sentence)]
        dishes = [s for s in spans if s in dishes_at]
        if not dishes:
            continue
        named = {s for s in spans if s in restaurants}
        for dish in dishes:
            if named and not any(restaurant in named for restaurant, _ in dishes_at[dish]):
                verdict.add("mismatched_pairing", f"{dish} is not offered by {', '.join(sorted(named))}")
        # With a restaurant named, the price must be that dish's price *there* (the same dish can cost
        # different amounts at two restaurants); otherwise any of its prices will do.
        known = []
        for dish in dishes:
            rows = [(r, p) for r, p in dishes_at[dish] if not named or r in named] or dishes_at[dish]
            known += [price for _, price in rows]
        for raw in _prices_in(sentence):
            value = float(raw)
            if any(abs(value - price) < 0.005 for price in known):
                continue
            if budget is not None and abs(value - budget) < 0.005:
                continue
            verdict.add("mismatched_price", f"${raw} is not the price of {', '.join(dishes)}")
    return verdict


def verify_food_answer(
    answer: str,
    *,
    names: set[str],
    prices: set[float],
    offers: list[tuple[str, str, float]] | None = None,
    budget: float | None = None,
    quantities: set[float] | None = None,
) -> Verdict:
    """Every bold name must be a name in the context; every price must be a price in it; and, given
    `offers`, a dish, its restaurant and its price must belong together (see `verify_pairings`). Given
    `quantities`, every "N minutes" / "N km" must be a figure the context carried (or the customer's own).

    `names` are already normalised; `prices` includes anything legitimately restated from the
    question (a stated budget), because "under $15" is not an invented price.
    """
    verdict = Verdict()
    families = name_families(names)
    for match in _BOLD.finditer(answer):
        span = normalize(match.group(1))
        if span in names or span in families or _is_figure(span):
            continue
        # A name inside a denial ("I can't find **X**", "there is no **X**") is not a claim that X exists. Claude
        # echoed the restaurant a customer asked about, bolded, while saying it had no information on it; the
        # strict check replaced that honest answer with a list. Only a negation right before the name counts, so
        # "there is no **X**, but **Y** is great" still has Y checked.
        if _negated_just_before(answer, match.start()):
            continue
        verdict.add("unknown_name", span)
    for raw in _prices_in(answer):
        if not any(abs(float(raw) - known) < 0.005 for known in prices):
            verdict.add("unknown_price", f"${raw}")
    if offers:
        pairing = verify_pairings(answer, offers=offers, budget=budget)
        verdict.ok = verdict.ok and pairing.ok
        verdict.violations += pairing.violations
    if quantities is not None:
        measured = verify_quantities(answer, quantities=quantities)
        verdict.ok = verdict.ok and measured.ok
        verdict.violations += measured.violations
    return verdict


def verify_quantities(answer: str, *, quantities: set[float]) -> Verdict:
    """Every "N minutes" / "N km" in an explanation must be a figure the facts carried."""
    verdict = Verdict()
    for raw in quantities_stated(answer):
        if not any(abs(float(raw) - known) < 0.05 for known in quantities):
            verdict.add("unknown_quantity", raw)
    return verdict


_NUMBER_WORDS = {
    word: value
    for value, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen twenty".split()
    )
}
_NUMBER = r"(\d+|" + "|".join(_NUMBER_WORDS) + r")"
_STATE = r"(?:pending|in[- ]progress|active|open|delivered|cancell?ed|completed|refunded)"
# "3 orders", "2 pending orders", "one delivered order" — a number in front of the word order(s), with at most
# a couple of describing words between. And "2 in progress", "1 delivered" — a number straight before a state.
_COUNT_CLAIMS = (
    re.compile(rf"\b{_NUMBER}\s+(?:[\w-]+\s+){{0,2}}orders?\b", re.I),
    re.compile(rf"\b{_NUMBER}\s+(?:are\s+|is\s+|of them\s+(?:are\s+)?)?{_STATE}\b", re.I),
)


def verify_counts(answer: str, *, counts: set[int]) -> Verdict:
    """Every "N orders" / "N delivered" claim must be a count the facts carried.

    Digits and spelled-out numbers are both read ("two pending orders"), because a model that was told to
    copy numbers exactly still writes small ones as words. Only a number sitting directly before the word
    order(s) or a status is a count claim, so an item quantity ("2 x Burger") or a price is left to its own check.
    """
    verdict = Verdict()
    for pattern in _COUNT_CLAIMS:
        for match in pattern.finditer(answer):
            raw = match.group(1).lower()
            value = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
            if value not in counts:
                verdict.add("unknown_count", raw)
    return verdict


def verify_prices(answer: str, *, prices: set[float]) -> Verdict:
    """Every price in an answer must be one the facts carried (a standalone form of the check inside
    `verify_food_answer`, for answers that have no dish names to check)."""
    verdict = Verdict()
    for raw in _prices_in(answer):
        if not any(abs(float(raw) - known) < 0.005 for known in prices):
            verdict.add("unknown_price", f"${raw}")
    return verdict


_REFUND = re.compile(r"\brefund(?:ed|s|ing)?\b", re.I)
_NOT_CHARGED = re.compile(
    r"\b(?:not|never)\s+(?:been\s+)?charged\b|n['’]t\s+(?:been\s+)?charged\b|\bno\s+charge\b", re.I
)


def verify_money(answer: str, *, money: str | None) -> Verdict:
    """A claim about the customer's money must be one the order's facts support.

    `money` is the explainer's own conclusion (`refunded`, `not_charged`, `refund_incomplete`, or None when
    nothing is recorded). Mentioning a refund needs `refunded` or `refund_incomplete`; saying the customer was
    not charged needs `not_charged`. With None, neither may appear: an explanation that promises a refund the
    platform never recorded is the costliest kind of invention this assistant could make.
    """
    verdict = Verdict()
    if _REFUND.search(answer) and money not in ("refunded", "refund_incomplete"):
        verdict.add("unsupported_refund_claim", "refund")
    if _NOT_CHARGED.search(answer) and money != "not_charged":
        verdict.add("unsupported_charge_claim", "not charged")
    return verdict


_CANCELLED = re.compile(r"\bcancel(?:l?ed|lation|lations|s)?\b", re.I)
_DELIVERED = re.compile(r"\bdelivered\b", re.I)


def verify_status(answer: str, *, status: str) -> Verdict:
    """A claim about how the order ended must match the order's recorded status.

    Found by running a real model: asked "why was my order cancelled?" about an order that was not, it answered
    "we do not have a recorded reason for the cancellation of your order" — adopting the question's false
    premise as fact. So "cancelled" needs a cancelled order (or `compensation_failed`, which is a cancellation
    whose refund failed) and "delivered" needs a delivered one. A negated sentence ("it hasn't been
    cancelled") is skipped: that is the *correct* answer to a false premise, and Claude gave it; rejecting it
    replaced a better answer with a plainer one. The soft spot is the same as the pairing check's.
    """
    verdict = Verdict()
    for sentence in _SENTENCE.split(answer):
        for pattern, word, allowed in (
            (_CANCELLED, "cancelled", status in ("cancelled", "compensation_failed")),
            (_DELIVERED, "delivered", status == "delivered"),
        ):
            for match in pattern.finditer(sentence):
                if allowed or _negated_just_before(sentence, match.start()):
                    continue
                verdict.add("unsupported_status_claim", word)
    return verdict


def _negated_just_before(sentence: str, position: int) -> bool:
    """Whether one of the three words before `position` is a negation.

    Close, because a negation elsewhere in the sentence says nothing about this word: "we do NOT have a
    recorded reason for the cancellation of your order" is negated about the reason, and still asserts a
    cancellation. "It hasn't been cancelled" is negated right at the word.
    """
    return bool(_NEGATION.search(" ".join(sentence[:position].split()[-3:])))


_NO_REASON = re.compile(
    r"(?:\bno|\bnot\s+(?:have|got)\s+a|n['’]t\s+have\s+a|\bwithout\s+a)\s+(?:recorded\s+|known\s+|clear\s+)?reason\b"
    r"|\breason\s+(?:is|was)\s+(?:not\s+|un)(?:recorded|known)\b",
    re.I,
)


def verify_cause(answer: str, *, cause: str | None) -> Verdict:
    """An explanation may not say there is no recorded reason when the facts carry one.

    Found by running a real local model on an order cancelled for `kitchen_timeout`: the facts said "the
    restaurant did not respond in time" and the model answered "we do not have a recorded reason" — the
    opposite of the truth, from a model that simply did not use what it was given. (The reverse, inventing a
    cause the facts lack, is what `verify_quantities` and the reason table already guard.)
    """
    verdict = Verdict()
    if cause and _NO_REASON.search(answer):
        verdict.add("contradicts_recorded_cause", "says there is no recorded reason")
    return verdict


_VERDICT = re.compile(
    r"\b(?:late|delay|delayed|delays|on[ -]time|behind schedule|overdue|early|slow|slowly|too long)\b", re.I
)


def verify_no_verdict(answer: str) -> Verdict:
    """An order explanation may not judge whether the order is late, on time, early or slow.

    The platform records no promised time, so there is nothing to compare against: "your order isn't late" and
    "it's still very early" are verdicts the facts cannot support. Claude gave both, then — on being told not
    to — wrote "Wait, I need to follow my rules" mid-answer and restated it. A prompt rule was not enough, and
    the words are detectable, so the check is deterministic. Not exempted for negation on purpose: "isn't late"
    *is* the verdict. Strict, so a model saying "I can't tell whether it is late" is replaced too, by the
    rule-written summary, which states what happened and when and judges nothing.

    Only for order explanations: in a food answer "late" or "slow" can be a dish name or a tag.
    """
    verdict = Verdict()
    match = _VERDICT.search(answer)
    if match:
        verdict.add("unsupported_verdict", match.group(0).lower())
    return verdict


def mentioned(answer: str, catalogue: dict[str, str]) -> list[str]:
    """The ids of catalogue entries (`normalised name -> id`) the answer names, in order, once each."""
    seen: list[str] = []
    for span in bold_names(answer):
        key = catalogue.get(span)
        if key is not None and key not in seen:
            seen.append(key)
    return seen
