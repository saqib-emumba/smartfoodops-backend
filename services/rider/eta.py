"""Arrival estimation for a dispatched order.

Pure arithmetic over three named constants, no I/O. The constants are named rather than
inlined because this is a mock estimate, and naming them keeps that honest instead of
burying 30 in a formula where it would read like a fact.
"""

# Assumed courier speed for the ETA below.
AVERAGE_COURIER_SPEED_KMH = 30.0
KITCHEN_PREP_PADDING_MINUTES = 5
MINIMUM_ETA_MINUTES = 5


def eta_minutes(distance_km: float | None) -> int:
    """Rough arrival estimate from the dispatch distance."""
    if distance_km is None:
        return MINIMUM_ETA_MINUTES
    travel = (distance_km / AVERAGE_COURIER_SPEED_KMH) * 60
    return max(MINIMUM_ETA_MINUTES, int(travel) + KITCHEN_PREP_PADDING_MINUTES)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle kilometres between two points — plenty accurate at delivery distances."""
    from math import asin, cos, radians, sin, sqrt

    d_lat, d_lon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * 6371.0088 * asin(sqrt(a))
