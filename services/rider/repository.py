"""PostgreSQL access for the `riders` table.

Dispatch is the interesting part of this file. It is one statement, not a read-then-write,
and that is deliberate: `FOR UPDATE SKIP LOCKED` means a concurrent dispatch for a different
order skips whatever row this one has locked and claims the next-nearest rider instead. The
race is prevented rather than detected, so neither caller ever has to retry a lost one.

The Week 2 blueprint's first revision read every available rider into Python, sorted them,
tried to claim the nearest, and returned `409` when a concurrent workflow had already taken
it — pushing the race back to the caller and re-running the whole search.

`user_id` is a plain UUID pointing into the User Service's database, so main.py verifies the
account over HTTP before the insert (D02, D18).
"""

from decimal import Decimal
from logging import Logger
from uuid import UUID

import psycopg2

from common.errors import conflict
from common.postgres import PostgresPool
from schemas import RiderRegisterRequest

_COLUMNS = (
    "id, user_id, vehicle_type, vehicle_number, is_available, "
    "current_latitude, current_longitude, current_order_id"
)

# Distance from the pickup point, in kilometres. Written once and interpolated into the
# three places the dispatch statement needs it, so the ORDER BY, the radius filter and the
# returned value can never be computed differently from one another.
_DISTANCE = (
    "haversine_km(current_latitude::double precision, "
    "current_longitude::double precision, %(lat)s, %(lon)s)"
)

_SELECT_BY_ID = f"SELECT {_COLUMNS} FROM riders WHERE id = %s"

_SELECT_BY_USER = f"SELECT {_COLUMNS} FROM riders WHERE user_id = %s"

_SELECT_BY_ORDER = f"""
    SELECT {_COLUMNS},
           NULL::double precision AS distance_km
      FROM riders
     WHERE current_order_id = %(order_id)s
"""

_INSERT_RIDER = f"""
    INSERT INTO riders
        (user_id, vehicle_type, vehicle_number, current_latitude, current_longitude)
    VALUES (%s, %s, %s, %s, %s)
    RETURNING {_COLUMNS}
"""

_UPDATE_LOCATION = f"""
    UPDATE riders
       SET current_latitude = %s,
           current_longitude = %s,
           updated_at = CURRENT_TIMESTAMP
     WHERE user_id = %s
    RETURNING {_COLUMNS}
"""

# Availability may not be toggled while an order is in hand: a rider cannot go off shift
# holding somebody's dinner. The saga is what releases them, on delivery or compensation.
_UPDATE_AVAILABILITY = f"""
    UPDATE riders
       SET is_available = %s,
           updated_at = CURRENT_TIMESTAMP
     WHERE user_id = %s
       AND current_order_id IS NULL
    RETURNING {_COLUMNS}
"""

_CLAIM_NEAREST = f"""
    UPDATE riders
       SET is_available = FALSE,
           current_order_id = %(order_id)s,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = (
             SELECT id
               FROM riders
              WHERE is_available
                AND current_order_id IS NULL
                AND current_latitude IS NOT NULL
                AND current_longitude IS NOT NULL
                AND {_DISTANCE} <= %(max_km)s
              ORDER BY {_DISTANCE}
              LIMIT 1
              FOR UPDATE SKIP LOCKED
           )
       AND is_available
    RETURNING {_COLUMNS}, {_DISTANCE} AS distance_km
"""

_RELEASE = f"""
    UPDATE riders
       SET is_available = TRUE,
           current_order_id = NULL,
           updated_at = CURRENT_TIMESTAMP
     WHERE current_order_id = %s
    RETURNING {_COLUMNS}
"""


def _to_float(row: dict | None) -> dict | None:
    """Convert psycopg2's `Decimal` coordinates to the floats the schemas declare.

    The columns are `DECIMAL(9,6)` because a coordinate is a fixed-precision quantity, but
    unlike money nothing arithmetic depends on it staying exact — D07's reasoning does not
    reach here, and Pydantic wants floats.
    """
    if row is None:
        return None
    return {
        key: float(value) if isinstance(value, Decimal) else value
        for key, value in row.items()
    }


class RiderRepository:
    def __init__(self, db: PostgresPool, *, logger: Logger):
        self._db = db
        self._logger = logger

    def find(self, rider_id: UUID) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_BY_ID, (str(rider_id),))
            return _to_float(cur.fetchone())

    def find_by_user(self, user_id: UUID) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(_SELECT_BY_USER, (str(user_id),))
            return _to_float(cur.fetchone())

    def register(self, payload: RiderRegisterRequest, user_id: UUID) -> dict:
        """Enrol a rider. `user_id` comes from the access token, not the body."""
        with self._db.cursor(commit=True) as cur:
            try:
                cur.execute(
                    _INSERT_RIDER,
                    (
                        str(user_id),
                        payload.vehicle_type,
                        payload.vehicle_number,
                        payload.current_latitude,
                        payload.current_longitude,
                    ),
                )
            except psycopg2.errors.UniqueViolation as exc:
                raise self._duplicate(exc, payload) from exc
            return _to_float(cur.fetchone())

    def update_location(self, user_id: UUID, lat: float, lon: float) -> dict | None:
        with self._db.cursor(commit=True) as cur:
            cur.execute(_UPDATE_LOCATION, (lat, lon, str(user_id)))
            return _to_float(cur.fetchone())

    def set_availability(self, user_id: UUID, is_available: bool) -> dict | None:
        """Toggle shift status. Returns None if the rider does not exist *or* is mid-order —
        main.py tells the two apart with a follow-up read, because they are different
        answers to the caller."""
        with self._db.cursor(commit=True) as cur:
            cur.execute(_UPDATE_AVAILABILITY, (is_available, str(user_id)))
            return _to_float(cur.fetchone())

    def dispatch(self, order_id: UUID, lat: float, lon: float, max_km: float) -> dict | None:
        """Claim the nearest available rider, or return the one already carrying this order.

        The prior-claim check is not an optimisation. Temporal retries activities, and a
        retry that skipped it would claim a *second* rider and strand the first — the
        partial unique index on `current_order_id` refuses the write, so the retry fails
        instead of succeeding, and the first rider stays held by a saga that thinks it has
        no rider. Checking first turns the retry into the no-op it should be.

        Both branches share one transaction so nothing can claim the order in between.

        Returns None when no rider is within range, which is a legitimate answer rather
        than an error — see DispatchResponse.
        """
        params = {"order_id": str(order_id), "lat": lat, "lon": lon, "max_km": max_km}
        with self._db.cursor(commit=True) as cur:
            cur.execute(_SELECT_BY_ORDER, {"order_id": str(order_id)})
            held = cur.fetchone()
            if held is not None:
                self._logger.info(
                    "Order %s is already held by rider %s; returning it unchanged",
                    order_id,
                    held["id"],
                )
                return _to_float(held)

            cur.execute(_CLAIM_NEAREST, params)
            return _to_float(cur.fetchone())

    def release(self, order_id: UUID) -> dict | None:
        """Free whichever rider holds this order.

        Idempotent: zero rows means nobody held it, which for a compensating action is
        success — the fleet is already in the state the caller asked for. Every failure
        path in the saga calls this, which is what stops a rider leaking `is_available =
        FALSE` when an order is cancelled after assignment.
        """
        with self._db.cursor(commit=True) as cur:
            cur.execute(_RELEASE, (str(order_id),))
            return _to_float(cur.fetchone())

    def _duplicate(
        self, exc: psycopg2.errors.UniqueViolation, payload: RiderRegisterRequest
    ) -> Exception:
        """Name what actually clashed, the way the User Service does for email vs phone."""
        constraint = exc.diag.constraint_name or ""
        if "user_id" in constraint:
            return conflict("This account is already registered as a rider")
        if "vehicle_number" in constraint:
            return conflict(
                f"Vehicle number '{payload.vehicle_number}' is already registered"
            )
        self._logger.info("Unexpected unique violation on riders: %s", constraint)
        return conflict("A rider with these details already exists")
