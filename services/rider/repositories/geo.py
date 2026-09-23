"""Redis GEO index for the fleet's live location — the Rider Service's read/write path for
`current_latitude`/`current_longitude`, moved out of Postgres (D49).

One sorted set for the whole fleet, not a key per rider: `GEOSEARCH ... BYRADIUS ... ASC
WITHDIST` is Redis's native equivalent of what `haversine_km` existed to do in SQL — a
radius filter, a distance sort and a limit, evaluated without pulling every candidate into
this service to sort them (`db/rider/init.sql`'s own comment on `haversine_km` states that
requirement; GEOSEARCH is the same requirement, answered by Redis instead of Postgres).

Availability is not tracked here — Redis only ever answers "nearest, regardless of busy or
free". The Postgres claim in `repositories/riders.py` is what turns a candidate list from
`nearby()` into an actual assignment; this store never decides anything, it only orders.

Unlike `MenuCache`, `set_location`/`nearby` raise rather than swallow a Redis failure: there
is no Postgres fallback left for either. A swallowed write would silently lose a rider's
position under a 200; a swallowed `nearby()` failure would misreport a Redis outage as an
empty fleet to the saga, which treats the two very differently (see apis/dispatch.py).
`get_location` does swallow — a profile read degrading to a null location is the same "no
position reported yet" state the schema already models.
"""

from logging import Logger
from uuid import UUID

import redis

from common.errors import service_unavailable
from common.redis_store import RedisStore

_KEY = "riders:geo"


class RiderGeoStore(RedisStore):
    def __init__(self, url: str, *, logger: Logger):
        super().__init__(url, logger=logger, name="Rider geo-index")

    def set_location(self, user_id: UUID, lat: float, lon: float) -> None:
        try:
            self._client.geoadd(_KEY, [lon, lat, str(user_id)])
        except redis.RedisError as exc:
            self._logger.error("Could not record location for rider %s: %s", user_id, exc)
            raise service_unavailable(
                "Could not record your location; please try again"
            ) from exc

    def get_location(self, user_id: UUID) -> tuple[float, float] | None:
        """The rider's last-reported position, or None on a miss or a Redis outage — both
        mean the same thing to a caller: no position to show."""
        try:
            positions = self._client.geopos(_KEY, str(user_id))
        except redis.RedisError as exc:
            self._logger.warning("Location lookup failed for rider %s: %s", user_id, exc)
            return None
        if not positions or positions[0] is None:
            return None
        lon, lat = positions[0]
        return float(lat), float(lon)

    def nearby(
        self, lat: float, lon: float, radius_km: float, count: int
    ) -> list[tuple[str, float]]:
        """The `count` nearest riders within `radius_km`, nearest first, as
        `(user_id, distance_km)` — availability unknown, callers must still filter."""
        try:
            results = self._client.geosearch(
                _KEY,
                longitude=lon,
                latitude=lat,
                radius=radius_km,
                unit="km",
                sort="ASC",
                count=count,
                withdist=True,
            )
        except redis.RedisError as exc:
            self._logger.error("Nearby-rider search failed: %s", exc)
            raise service_unavailable(
                "Could not search for a nearby rider; please try again"
            ) from exc
        return [(member, float(distance)) for member, distance in results]
