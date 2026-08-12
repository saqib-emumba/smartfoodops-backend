"""SmartFoodOps Order Service — idempotent checkout (Port 8004).

Owns the PostgreSQL `orders` table. Prices are always recalculated server-side from the
Menu Service's published menu, and audit logs are written through the Menu Service so that
this service never talks to MongoDB directly.
"""

import json
import logging
import os
from collections.abc import Iterable
from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal
from uuid import UUID

import httpx
import psycopg2
from fastapi import FastAPI, Header, HTTPException, Response, status
from psycopg2 import pool
from psycopg2.extras import Json, RealDictCursor

from schemas import OrderCreateRequest, OrderItemSelection, OrderResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("order-service")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://sfo_admin:sfo_password_123@db-postgres:5432/smartfoodops_core",
)
MENU_SERVICE_URL = os.getenv("MENU_SERVICE_URL", "http://menu-service:8003")
SERVICE_NAME = "order-service"
HTTP_TIMEOUT = 5.0
CENTS = Decimal("0.01")

db_pool = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global db_pool
    db_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)
    logger.info("PostgreSQL connection pool initialised")
    try:
        yield
    finally:
        db_pool.closeall()
        db_pool = None


app = FastAPI(title="SmartFoodOps Order Service", lifespan=lifespan)


@contextmanager
def db_cursor(commit: bool = False):
    """Lease a pooled connection. A starved/unreachable pool surfaces as 500."""
    if db_pool is None:
        raise HTTPException(status_code=500, detail="Database pool is not initialised")
    try:
        conn = db_pool.getconn()
    except (pool.PoolError, psycopg2.OperationalError) as exc:
        logger.error("Could not lease a PostgreSQL connection: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Database connection pool exhausted; order was not created",
        ) from exc
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit() if commit else conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)


def unprocessable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def fetch_menu(restaurant_id: UUID) -> dict:
    """Pull the restaurant's published menu from the Menu Service."""
    url = f"{MENU_SERVICE_URL}/api/v1/menus/{restaurant_id}"
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.get(url)
    except httpx.RequestError as exc:
        logger.error("Menu Service unreachable at %s: %s", url, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Menu Service is unreachable; cannot validate the order",
        ) from exc

    if response.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active menu found for restaurant {restaurant_id}",
        )
    if response.status_code != status.HTTP_200_OK:
        logger.error(
            "Unexpected Menu Service response %s: %s",
            response.status_code,
            response.text[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected response from Menu Service",
        )
    return response.json()


def flatten_catalogue(menu: dict) -> dict:
    """Index every menu item by item_id across all categories."""
    return {
        item["item_id"]: item
        for category in menu.get("categories", [])
        for item in category.get("items", [])
    }


def selected_names(raw) -> list[str]:
    """Normalise a customization selection into a list of option names."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, Iterable):
        raise unprocessable(f"Unsupported customization selection: {raw!r}")
    names = []
    for entry in raw:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and "name" in entry:
            names.append(entry["name"])
        else:
            raise unprocessable(f"Unsupported customization selection: {entry!r}")
    return names


def price_line(item: dict, selection: OrderItemSelection) -> tuple[dict, Decimal]:
    """Recalculate one line's price from the menu, rejecting anything inconsistent."""
    if not item.get("is_available", False):
        raise unprocessable(f"Item '{selection.item_id}' is currently unavailable")

    unit_price = Decimal(str(item["base_price"]))
    groups = {g["group_id"]: g for g in item.get("customization_groups", [])}
    customizations = selection.customizations or {}
    chosen_options: list[dict] = []

    for group_id, raw in customizations.items():
        group = groups.get(group_id)
        if group is None:
            raise unprocessable(
                f"Item '{selection.item_id}' has no customization group '{group_id}'"
            )
        names = selected_names(raw)
        if not group["min_selection"] <= len(names) <= group["max_selection"]:
            raise unprocessable(
                f"Group '{group_id}' on item '{selection.item_id}' accepts between "
                f"{group['min_selection']} and {group['max_selection']} selections, got {len(names)}"
            )
        options = {option["name"]: option for option in group["options"]}
        for name in names:
            option = options.get(name)
            if option is None:
                raise unprocessable(
                    f"Option '{name}' is not offered by group '{group_id}' on item '{selection.item_id}'"
                )
            extra = Decimal(str(option.get("extra_price", 0)))
            unit_price += extra
            chosen_options.append(
                {"group_id": group_id, "name": name, "extra_price": float(extra)}
            )

    for group_id, group in groups.items():
        if group["min_selection"] > 0 and group_id not in customizations:
            raise unprocessable(
                f"Group '{group_id}' on item '{selection.item_id}' requires at least "
                f"{group['min_selection']} selection(s)"
            )

    unit_price = unit_price.quantize(CENTS)
    line_total = (unit_price * selection.quantity).quantize(CENTS)
    snapshot = {
        "item_id": selection.item_id,
        "name": item.get("name"),
        "quantity": selection.quantity,
        "customizations": selection.customizations,
        "unit_price": float(unit_price),
        "line_total": float(line_total),
        "selected_options": chosen_options,
    }
    return snapshot, line_total


def build_order_snapshot(
    menu: dict, payload: OrderCreateRequest
) -> tuple[list[dict], Decimal]:
    """Validate availability and recompute the authoritative total for the checkout."""
    catalogue = flatten_catalogue(menu)
    snapshot: list[dict] = []
    total = Decimal("0.00")

    for selection in payload.items:
        item = catalogue.get(selection.item_id)
        if item is None:
            raise unprocessable(
                f"Item '{selection.item_id}' is not on the restaurant's active menu"
            )
        line, line_total = price_line(item, selection)
        snapshot.append(line)
        total += line_total

    total = total.quantize(CENTS)
    claimed = Decimal(str(payload.total_amount)).quantize(CENTS)
    if total != claimed:
        raise unprocessable(
            f"total_amount mismatch: client sent {claimed}, server recalculated {total}"
        )
    return snapshot, total


def find_order_by_idempotency_key(cur, key: str) -> dict | None:
    cur.execute(
        """
        SELECT id, customer_id, restaurant_id, items, total_amount, status, idempotency_key
        FROM orders
        WHERE idempotency_key = %s
        """,
        (key,),
    )
    return cur.fetchone()


def write_audit_log(order: dict, idempotency_key: str) -> None:
    """Persist the 'created' transition through the Menu Service logging endpoint.

    Best-effort: the order is already committed, so a logging failure is reported but does
    not fail the client's request.
    """
    raw_log = json.dumps(
        {
            "event": "order_created",
            "order_id": str(order["id"]),
            "total_amount": float(order["total_amount"]),
            "items_count": len(order["items"]),
        }
    )
    body = {
        "order_id": str(order["id"]),
        "status": "created",
        "service": SERVICE_NAME,
        "raw_log": raw_log,
        "updated_by": "customer_client",
        "metadata": {"idempotency_key": idempotency_key},
    }
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(f"{MENU_SERVICE_URL}/api/v1/menus/logs", json=body)
        if response.status_code >= 400:
            logger.error(
                "Menu Service rejected audit log (%s): %s",
                response.status_code,
                response.text[:200],
            )
    except httpx.RequestError as exc:
        logger.error("Could not deliver audit log to Menu Service: %s", exc)


@app.get("/api/v1/orders/health")
def health():
    db_ok = True
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
    except Exception as exc:  # pragma: no cover - health must never raise
        logger.warning("Health check database probe failed: %s", exc)
        db_ok = False
    return {
        "status": "Orders Service operational",
        "service": SERVICE_NAME,
        "database_reachable": db_ok,
        "menu_service_url": MENU_SERVICE_URL,
    }


@app.post(
    "/api/v1/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_order(
    payload: OrderCreateRequest,
    response: Response,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
) -> OrderResponse:
    """Create an order idempotently after re-pricing it against the live menu."""
    if not x_idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header is required",
        )

    # (b) Replay protection — an already-seen key returns the stored order untouched.
    with db_cursor() as cur:
        existing = find_order_by_idempotency_key(cur, x_idempotency_key)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        logger.info("Idempotent replay for key %s", x_idempotency_key)
        return OrderResponse(**existing)

    # (c) Re-price from the Menu Service; unavailable items or a total mismatch abort here.
    menu = fetch_menu(payload.restaurant_id)
    items_snapshot, total = build_order_snapshot(menu, payload)

    with db_cursor(commit=True) as cur:
        try:
            cur.execute(
                """
                INSERT INTO orders (customer_id, restaurant_id, items, total_amount, status, idempotency_key)
                VALUES (%s, %s, %s, %s, 'created', %s)
                RETURNING id, customer_id, restaurant_id, items, total_amount, status, idempotency_key
                """,
                (
                    str(payload.customer_id),
                    str(payload.restaurant_id),
                    Json(items_snapshot),
                    total,
                    x_idempotency_key,
                ),
            )
        except psycopg2.errors.UniqueViolation as exc:
            # Concurrent submission won the race — fall back to the stored order.
            logger.info("Concurrent replay for key %s", x_idempotency_key)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An order with this idempotency key is already being processed",
            ) from exc
        except psycopg2.errors.ForeignKeyViolation as exc:
            constraint = getattr(exc.diag, "constraint_name", None) or ""
            subject = "restaurant" if "restaurant" in constraint else "customer"
            raise unprocessable(
                f"Unknown {subject} referenced by this order"
            ) from exc
        order = cur.fetchone()

    # (d) Audit trail is written through the Menu Service, never straight to MongoDB.
    write_audit_log(order, x_idempotency_key)

    return OrderResponse(**order)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8004)
