# Rider Live Location: Postgres → Redis — Design

Status: **implemented** — see [D49](key-decisions.md#d49--rider-live-location-moves-from-postgres-columns-to-a-redis-geo-index)
for the changelog-style record of what shipped. This doc maps the design for moving rider live
location out of Postgres and into Redis, and what stays behind.

## 1. The problem

`riders.current_latitude`/`current_longitude` (`db/rider/init.sql:50-51`, `DECIMAL(9,6)`) are written
on every location ping — in production, thousands of riders every few seconds. That's write-hot,
ephemeral, loseable-on-restart data with no transactional requirement, yet it lives in Postgres
today, where every "update" is a new MVCC tuple: sustained ping traffic means constant dead-tuple
churn and autovacuum pressure on the `riders` table, competing with the dispatch query's own
`FOR UPDATE SKIP LOCKED` lock on that same table (`repositories/riders.py::_CLAIM_NEAREST`). Redis —
already running in this stack for the menu cache and refresh tokens — is the right home for
write-hot, disposable state: in-memory, fast writes, and losing a stale ping on restart is harmless.

**What must not move:** `is_available`/`current_order_id` and the claim itself. That's the one place
in this service needing a real transactional guarantee — `FOR UPDATE SKIP LOCKED` plus the unique
partial index `idx_riders_current_order` (`db/rider/init.sql:75-77`) is what gives "at most one rider
per order" even under a retried Temporal dispatch activity (D29). Redis has no equivalent primitive
without reimplementing it, so that stays exactly where it is.

**End state:** location lives in a Redis GEO index; the dispatch claim stays one Postgres statement,
now restricted to a candidate list Redis hands it.

## 2. Redis structure: one GEO sorted set for the whole fleet

New key `riders:geo` (member = `user_id`), written via `GEOADD` and queried via
`GEOSEARCH ... BYRADIUS ... ASC COUNT <cap> WITHDIST` / `GEOPOS`.

Not a hash-per-rider with distance computed in Python — that would recreate exactly what
`haversine_km`'s own comment (`db/rider/init.sql:20-24`) says it exists to avoid: reading every
candidate into the service to sort them, which is what makes the row lock in the claim query
impossible to express. GEO's radius filter + distance sort + limit is the direct Redis-native
equivalent, evaluated inside Redis, and it also serves the per-rider read (`GEOPOS riders:geo
<user_id>`) for free — one structure covers both the fleet-wide dispatch query and the profile read.

New store, `services/rider/repositories/geo.py`, mirroring `services/menu/repositories/cache.py`'s
shape (subclass of `services/common/redis_store.py::RedisStore`):

```python
class RiderGeoStore(RedisStore):
    def __init__(self, url: str, *, logger: Logger):
        super().__init__(url, logger=logger, name="Rider geo-index")

    def set_location(self, user_id: UUID, lat: float, lon: float) -> None: ...  # GEOADD
    def get_location(self, user_id: UUID) -> tuple[float, float] | None: ...    # GEOPOS
    def nearby(self, lat: float, lon: float, radius_km: float, count: int) -> list[tuple[str, float]]: ...  # GEOSEARCH
```

**`set_location`/`nearby` raise `service_unavailable(...)` on `redis.RedisError`; `get_location`
swallows it and returns `None`.** This is a deliberate asymmetry from `MenuCache`, where every method
swallows because Postgres is always the fallback. Here there is no Postgres fallback left for
location:
- A swallowed write would silently lose a rider's position under a `200` response.
- A swallowed `nearby()` failure would misreport a Redis outage as "empty fleet" to the saga —
  `apis/dispatch.py`'s own docstring already distinguishes the two: `no_rider_in_range` means
  wait-and-retry, `503` means the service itself is broken. Conflating them would make the saga
  retry the wrong thing.
- A profile read degrading to a null location, by contrast, is the same "no position reported yet"
  state the schema already models — safe to swallow.

Config additions (`services/common/config.py`, beside `DEFAULT_AUTH_REDIS_URL`):
- `DEFAULT_RIDER_REDIS_URL = "redis://cache-redis:6379/2"` — logical DB 2 (menu cache = 0, user
  refresh tokens = 1).
- `RIDER_DISPATCH_CANDIDATE_CAP = 50`, beside `RIDER_MAX_DISTANCE_KM` — bounds the candidate list
  `nearby()` hands to Postgres. See §3 for why this is the one real approximation this design
  introduces.

`services/rider/requirements.txt` gains `redis==5.2.1` (same pin as `services/menu/requirements.txt`).

## 3. Dispatch claim: two steps, and where the one real approximation lives

**Step 1 (Redis, read-only, in `apis/dispatch.py`):**
`deps.geo.nearby(restaurant_lat, restaurant_lon, max_km, RIDER_DISPATCH_CANDIDATE_CAP)` returns a
distance-ordered `[(user_id, distance_km), ...]` — Redis has no notion of availability, so this list
can include busy riders interleaved with idle ones.

**Step 2 (Postgres, the claim, in `repositories/riders.py`):** `dispatch()` takes the ordered
`user_id` list instead of raw lat/lon:

```sql
UPDATE riders
   SET is_available = FALSE, current_order_id = %(order_id)s, updated_at = CURRENT_TIMESTAMP
 WHERE id = (
         SELECT id FROM riders
          WHERE is_available AND current_order_id IS NULL
            AND user_id = ANY(%(candidate_user_ids)s::uuid[])
          ORDER BY array_position(%(candidate_user_ids)s::uuid[], user_id)
          LIMIT 1
          FOR UPDATE SKIP LOCKED
       )
   AND is_available
RETURNING id, user_id, vehicle_type, vehicle_number, is_available, current_order_id
```

**Why this is exact, not approximate, for the riders it considers:** `array_position` over a fixed
array is a deterministic restatement of Redis's distance order. Filtering out busy rows via
`WHERE is_available` before that `ORDER BY` runs can never reorder what survives, so the nearest
*available* candidate among the ones considered is picked correctly every time — availability stays
Postgres's sole, transactional authority; Redis only ever contributes candidate identity and order.

**Where the real approximation lives:** the `COUNT` cap. Today's Postgres query scans every row
satisfying the WHERE clause, so it can never miss an eligible rider. Once Redis truncates to the
nearest N by raw distance *before* availability is known, a pathological cluster — 50+ busy riders
all strictly closer than one available rider, all within `max_km` — can make step 1 return zero
available candidates, so dispatch answers `no_rider_in_range` even though a rider exists further out
but still in range. This is a genuine, structural behavior change versus today, not a race-window
artifact, and it's recorded as an accepted, bounded cost in D49 rather than glossed over.

There's also a small, separate staleness race: a rider can move or go offline in the milliseconds
between the Redis read and the Postgres claim. Harmless — `WHERE is_available` still gates
correctness; worst case a slightly-stale-position rider is picked over a marginally closer one, never
a wrong or duplicate claim.

`distance_km` for the response no longer comes from Postgres — `haversine_km` and the `_DISTANCE` SQL
fragment are gone. The handler matches the claimed row's `user_id` back against the Redis-returned
`(user_id, distance_km)` pairs. The existing "already-held" branch (`_SELECT_BY_ORDER`) already
returns `distance_km = NULL` today, unchanged.

## 4. What gets dropped from Postgres

| Object | Fate | Why |
|---|---|---|
| `haversine_km` (`init.sql:20-38`) | Dropped | Nothing computes distance in Postgres any more. |
| `idx_riders_dispatchable` (`init.sql:66-68`) | Dropped | Existed to qualify location-bearing rows ahead of an in-SQL distance sort that no longer happens; the claim now filters by `user_id = ANY(candidates)` (≤50 rows) against the existing unique index on `user_id`. |
| `idx_riders_current_order` (`init.sql:75-77`) | Unchanged | Orthogonal to location — protects the at-most-one-rider-per-order invariant regardless of where coordinates live. |
| `current_latitude`/`current_longitude` columns | Dropped | Clean cutover, same precedent as D48 dropping `users.role_id` outright rather than keeping a legacy column alongside the new source of truth. |

## 5. Write path

- **Registration** (`apis/profile.py::register_rider`): the Postgres insert drops the two lat/lon
  params. After the Postgres commit, if the request supplied a location, call
  `deps.geo.set_location(user_id, lat, lon)`. `RiderRegisterRequest` (`schemas/riders.py:9-17`) gets
  a both-or-neither validator — today's independently-nullable columns tolerated a half-set pair only
  because they were two independent SQL columns; a GEO member has no such partial state, and
  `RiderLocationRequest` already states this same rule for pings.
- **Location ping** (`PATCH /api/v1/riders/me/location`): becomes Redis-only. `update_location`/
  `_UPDATE_LOCATION` are deleted outright (no unused path left behind, D48-style clean cutover). The
  handler looks up the row via the existing `own_profile()` Postgres read (still the 404 check),
  calls `deps.geo.set_location(...)`, and merges the just-written lat/lon into the response directly
  — no redundant `GEOPOS` round trip. `riders.updated_at` now reflects only availability/dispatch
  changes, never a location ping — a small, worth-naming semantic shift.

## 6. Read path: merge without leaking storage details into the schema

`RiderResponse` (`schemas/riders.py:35-45`) keeps declaring `current_latitude`/`current_longitude` as
plain optional floats — callers never learn one field is Postgres and one is Redis. A new
`services/rider/fleet.py::with_location(row, geo)` merges `geo.get_location(row["user_id"])` into the
row, failing soft to `(None, None)` on a cache miss or outage — a null location is already a
legitimate, modeled state. Called from `get_own_profile` and `set_availability`, both of which
currently return a `RiderResponse` straight off a Postgres row and must not silently null out a
location the rider actually has.

## 7. Wiring

- `deps.py`: `RIDER_REDIS_URL = service_url("RIDER_REDIS_URL", DEFAULT_RIDER_REDIS_URL)`; `geo =
  RiderGeoStore(RIDER_REDIS_URL, logger=logger)`.
- `main.py`: a `_geo_lifespan` exactly like Menu Service's `_cache_lifespan` (`connect()`/`close()`),
  folded into `compose_lifespan(deps.db.lifespan, deps.temporal.lifespan, _geo_lifespan)`.
- `apis/health.py`: `location_cache_reachable=deps.geo.is_reachable()` added to `health_payload(...)`,
  alongside the existing `temporal_reachable`.
- `docker-compose.yml`: `rider-service` gains `RIDER_REDIS_URL: redis://cache-redis:6379/2` and
  `depends_on: cache-redis: condition: service_healthy` (matching `user-service`'s block). Mirrored
  byte-for-byte into `scripts/init_bootstrap.sh`'s heredoc, diff-verified.

## 8. Existing-deployment migration

This crosses Postgres and Redis, so it can't be pure SQL the way `db/user/backfill_user_roles.sql`
was. Two artifacts:

**`scripts/migrate_rider_locations_to_redis.sh`** (bash, run once by hand against the live stack):
1. Read every non-null `(user_id, current_latitude, current_longitude)` from `sfo-rider-db` via
   `psql`.
2. For each row, `GEOADD riders:geo <lon> <lat> <user_id>` against `sfo-redis` (DB 2).
3. Verify: row count from Postgres matches `ZCARD riders:geo`; spot-check a few rows via `GEOPOS`
   within a small tolerance (GEO's geohash quantization is coarser than, but consistent with,
   `DECIMAL(9,6)` — don't expect bit-exact equality). Abort loudly on mismatch.

**`db/rider/drop_rider_location_columns.sql`** — gated like `backfill_user_roles.sql` (verification
commented above the DROP, run manually after the migration script confirms a match):
```sql
-- Run only after scripts/migrate_rider_locations_to_redis.sh reports a verified match.
BEGIN;
ALTER TABLE riders DROP COLUMN current_latitude, DROP COLUMN current_longitude;
DROP INDEX IF EXISTS idx_riders_dispatchable;
DROP FUNCTION IF EXISTS haversine_km(double precision, double precision, double precision, double precision);
COMMIT;
```

**Sequencing:** old code writes those columns on every ping; new code never touches them, so the
column drop must never precede the new code going live:
1. Run the backfill script immediately before redeploying (minimizes the gap; any ping landing
   in between is lost — consistent with this data's already-accepted loseable tolerance).
2. Deploy the new rider-service image.
3. Confirm health (`location_cache_reachable: true`) and run the full smoke suite.
4. Run `drop_rider_location_columns.sql` — safely deferrable as a rollback buffer, but never before
   step 2.

## 9. Test plan

`scripts/smoke-test.sh`:
- Dual-rider tie-breaking registration/location assertions (~lines 699-717): unchanged — the HTTP
  contract is identical.
- The two direct-`psql` groundings (~726-731, ~865-869) only touch `is_available`/`current_order_id`,
  which never move — unchanged.
- **New**: right after the location-ping assertion, `docker exec sfo-redis redis-cli -n 2 GEOPOS
  riders:geo "$RIDER_USER_ID"` asserting a non-null coordinate pair — the Redis-side grounding step
  this migration needs, cheap insurance against `set_location` silently no-op'ing.
- **New**: since both smoke riders share identical coordinates, assert `distance_km == 0.0` on the
  dispatch/assignment check — a regression guard on the Redis→Postgres candidate-order handoff, the
  newest and most fragile part of this design.
- Run the full suite twice, matching the D48 migration precedent: once against a fresh
  `init_bootstrap.sh` bootstrap, once against a stack migrated via
  `migrate_rider_locations_to_redis.sh` + `drop_rider_location_columns.sql` with the new image
  deployed on top.

## 10. What this doesn't do

- No per-entry staleness/TTL on the geo set — unchanged from today, where a reported position was
  also trusted indefinitely until overwritten; not a new gap introduced by this migration.
- No fix for the `COUNT` cap's structural approximation (§3) — accepted as bounded, not eliminated.
- Redis becomes a hard, blocking dependency for dispatch specifically (a `503`, not a graceful
  degradation, unlike the menu cache's fallback-to-Postgres or the refresh store's forced-relogin).
  This extends an already-operated dependency's blast radius into a service that had none on Redis
  before — it doesn't add new infrastructure to the stack, but it does raise this one codepath's
  exposure to a Redis outage from "none" to "total."

## 11. Decision record

See [D49](key-decisions.md#d49--rider-live-location-moves-from-postgres-columns-to-a-redis-geo-index)
— the changelog-style summary of what actually shipped: Decided / Instead of (all-Postgres as
before; all-Redis including the claim; a Lua-scripted atomic claim mirroring availability into
Redis) / Why / Cost, per the format every prior decision uses.
