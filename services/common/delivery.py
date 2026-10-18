"""A delivery-time estimate, the way a food-delivery app shows one ("20-30 min") (D68).

Pure arithmetic over named constants, no I/O. Every figure here is a stated assumption, not a measurement, and
that is why they are constants with comments rather than numbers buried in a formula: the platform records no
delivery address on an order and does no real routing, and a restaurant's preparation time is one figure its owner sets,
not a measurement. What it does have is a
courier's distance and the restaurant's and (when the customer shares it) the customer's coordinates. The
estimate is:

    preparation (the restaurant's own, set by its owner)  +  the courier reaching the restaurant  +  the trip from
    the restaurant to the customer (real, from the customer's shared location, else a typical one)

shown as a ten-minute window around the total, because a single number would claim a precision nothing here has.
It is an estimate, never a promise: the assistant is told to say so, and to say when the trip was assumed.

Shared by the AI Service's food path (a restaurant's estimate before an order exists) and its order path (the time
left on an order already placed), so the two cannot disagree about what a delivery should take.
"""

# The same assumed courier speed the dispatch ETA uses (`rider/eta.py`): this is a mock, and naming it keeps that honest.
AVERAGE_SPEED_KMH = 30.0

# How long a kitchen is assumed to take when a restaurant's own figure is not available. The real one lives in the
# Restaurant Service's database (`restaurants.prep_time_minutes`, set by the owner); this is only the fallback, and
# the same number as that column's default.
PREP_MINUTES = 15

# Used only when the real figure is unknown. A courier not yet assigned has no distance; an order carries no delivery
# address, so without a shared location the trip to the customer is a typical one.
ASSUMED_COURIER_KM = 1.5
ASSUMED_TRIP_KM = 3.0

# Beyond this the restaurant does not deliver to that customer: no estimate is given, and the answer says so.
MAX_DELIVERY_KM = 15.0

# The estimate is shown as `centre +/- WINDOW`, the centre rounded to the nearest five minutes.
WINDOW_MINUTES = 5
ROUNDING_MINUTES = 5


def _travel_minutes(km: float) -> float:
    return km / AVERAGE_SPEED_KMH * 60


def delivery_estimate(
    courier_km: float | None = None, trip_km: float | None = None, prep_minutes: int | None = None
) -> dict:
    """`{minutes_low, minutes_high, prep_minutes, trip_assumed}`, or `{out_of_range: True}` beyond delivery range.

    `courier_km`: how far the courier is from the restaurant, if known. `trip_km`: how far the restaurant is from the
    customer, if known (from a shared location). `prep_minutes`: the restaurant's own preparation time, if known.
    Any of them missing falls back to its named assumption.
    """
    prep = PREP_MINUTES if prep_minutes is None else prep_minutes
    if trip_km is not None and trip_km > MAX_DELIVERY_KM:
        return {"out_of_range": True}
    total = (
        prep
        + _travel_minutes(ASSUMED_COURIER_KM if courier_km is None else courier_km)
        + _travel_minutes(ASSUMED_TRIP_KM if trip_km is None else trip_km)
    )
    centre = int(round(total / ROUNDING_MINUTES)) * ROUNDING_MINUTES
    return {
        "minutes_low": max(centre - WINDOW_MINUTES, ROUNDING_MINUTES),
        "minutes_high": centre + WINDOW_MINUTES,
        "prep_minutes": prep,
        "trip_assumed": trip_km is None,
    }


def minutes_remaining(estimate: dict, elapsed_minutes: int) -> dict:
    """The same window with the time already spent taken off, never below a 0-5 minute floor.

    An order past its window reads "0-5 minutes to go" rather than a negative or a verdict: the platform has
    no promised time to be late against, only an estimate that has run its course.
    """
    if estimate.get("out_of_range"):
        return estimate
    low = max(estimate["minutes_low"] - elapsed_minutes, 0)
    high = max(estimate["minutes_high"] - elapsed_minutes, ROUNDING_MINUTES)
    return {
        "minutes_low": low,
        "minutes_high": high,
        "prep_minutes": estimate["prep_minutes"],
        "trip_assumed": estimate["trip_assumed"],
    }
