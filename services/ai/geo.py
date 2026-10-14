"""Great-circle distance, for ranking restaurants against a customer's location (D62)."""

from math import asin, cos, radians, sin, sqrt

_EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Kilometres between two points. Plenty accurate at delivery distances; not for navigation."""
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))
