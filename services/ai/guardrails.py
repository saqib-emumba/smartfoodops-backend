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

What it does not catch: a false claim made without a bold name, a price or a quantity (an
invented ingredient in prose, say). The prompt is the only guard there, which is why it also
forbids it, and why the ceiling is stated rather than hidden.
"""

import re
from dataclasses import dataclass, field

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_PRICE = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)")
_QUANTITY = re.compile(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|km)\b", re.I)
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


def bold_names(answer: str) -> list[str]:
    return [normalize(span) for span in _BOLD.findall(answer)]


def verify_food_answer(answer: str, *, names: set[str], prices: set[float]) -> Verdict:
    """Every bold name must be a name in the context; every price must be a price in it.

    `names` are already normalised; `prices` includes anything legitimately restated from the
    question (a stated budget), because "under $15" is not an invented price.
    """
    verdict = Verdict()
    for span in bold_names(answer):
        if span not in names:
            verdict.add("unknown_name", span)
    for raw in _PRICE.findall(answer):
        if not any(abs(float(raw) - known) < 0.005 for known in prices):
            verdict.add("unknown_price", f"${raw}")
    return verdict


def verify_quantities(answer: str, *, quantities: set[float]) -> Verdict:
    """Every "N minutes" / "N km" in an explanation must be a figure the facts carried."""
    verdict = Verdict()
    for raw in _QUANTITY.findall(answer):
        if not any(abs(float(raw) - known) < 0.05 for known in quantities):
            verdict.add("unknown_quantity", raw)
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


def mentioned(answer: str, catalogue: dict[str, str]) -> list[str]:
    """The ids of catalogue entries (`normalised name -> id`) the answer names, in order, once each."""
    seen: list[str] = []
    for span in bold_names(answer):
        key = catalogue.get(span)
        if key is not None and key not in seen:
            seen.append(key)
    return seen
