"""Raw-SQL repository for insurance_orders + insurance_order_status_history.

All status changes go through set_status(), which enforces the state
machine and writes a history row — callers never UPDATE status directly.
"""

import sqlite3
from datetime import date, datetime, timezone

from app.orders.models import Order
from app.orders.numbering import public_number
from app.orders.state_machine import OrderStatus, ensure_transition_allowed
from app.tokens import new_resume_token


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_order(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    country_code: str,
    vehicle_category_code: str,
    period_code: str,
    start_date: date,
    end_date: date,
    price_customer_minor: int,
    data_entry_method: str,
    registration_number: str,
    identifier_type: str,
    identifier: str,
    manufacturer_id: int,
    manufacturer_name: str,
    model_id: int,
    model_name: str,
    full_name: str,
    contact_type: str,
    contact_value: str,
    customer_currency: str,
    purchase_currency: str,
) -> Order:
    """Creates an order already holding the full pre-order draft: category,
    period, dates, price, vehicle catalog data and the policyholder's
    contact — everything is collected before the order exists (see
    app.sessions.repository draft storage), so by construction the order
    starts in DRAFT and is transitioned to DATA_COMPLETED immediately,
    producing a real (if instantaneous) status-history entry rather than
    skipping the state machine.

    manufacturer_name/model_name are written into the legacy vehicle_make/
    vehicle_model columns as a point-in-time SNAPSHOT, not just resolved via
    a live join to the catalog — if a manufacturer/model later gets
    renamed or deactivated in the catalog, this order's summary must keep
    showing what the customer actually saw and bought (see summary.html,
    which reads these columns directly rather than joining catalog tables).
    manufacturer_id/model_id are still stored too, for any future catalog-
    relative use (analytics, re-sync), but display always prefers the
    snapshot.
    """
    now = _now()
    resume_token = new_resume_token()

    cursor = conn.execute(
        """
        INSERT INTO insurance_orders (
            public_number, country_code, status, session_id,
            vehicle_category_code, period_code, start_date, end_date, price_customer_minor,
            data_entry_method, car_number, identifier_type, identifier,
            manufacturer_id, vehicle_make, model_id, vehicle_model,
            full_name, contact_type, contact_value,
            customer_currency, purchase_currency,
            resume_token, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "",  # public_number filled in below once we have the id
            country_code,
            OrderStatus.DRAFT.value,
            session_id,
            vehicle_category_code,
            period_code,
            start_date.isoformat(),
            end_date.isoformat(),
            price_customer_minor,
            data_entry_method,
            registration_number,
            identifier_type,
            identifier,
            manufacturer_id,
            manufacturer_name,
            model_id,
            model_name,
            full_name,
            contact_type,
            contact_value,
            customer_currency,
            purchase_currency,
            resume_token,
            now,
            now,
        ),
    )
    order_id = cursor.lastrowid
    conn.execute(
        "UPDATE insurance_orders SET public_number = ? WHERE id = ?",
        (public_number(order_id), order_id),
    )
    conn.execute(
        """
        INSERT INTO insurance_order_status_history (order_id, from_status, to_status, note, created_at)
        VALUES (?, NULL, ?, 'order created', ?)
        """,
        (order_id, OrderStatus.DRAFT.value, now),
    )
    conn.commit()

    order = get_order_by_id(conn, order_id)
    assert order is not None
    set_status(conn, order.id, OrderStatus.DATA_COMPLETED, note="category/period/vehicle/policyholder data present at creation")
    order = get_order_by_id(conn, order_id)
    assert order is not None
    return order


def get_order_by_id(conn: sqlite3.Connection, order_id: int) -> Order | None:
    row = conn.execute("SELECT * FROM insurance_orders WHERE id = ?", (order_id,)).fetchone()
    return Order.from_row(row) if row else None


def get_order_by_token(conn: sqlite3.Connection, resume_token: str) -> Order | None:
    row = conn.execute("SELECT * FROM insurance_orders WHERE resume_token = ?", (resume_token,)).fetchone()
    return Order.from_row(row) if row else None


def set_status(conn: sqlite3.Connection, order_id: int, new_status: OrderStatus, *, note: str | None = None) -> None:
    order = get_order_by_id(conn, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")

    current = OrderStatus(order.status)
    ensure_transition_allowed(current, new_status)

    now = _now()
    conn.execute(
        "UPDATE insurance_orders SET status = ?, updated_at = ? WHERE id = ?",
        (new_status.value, now, order_id),
    )
    conn.execute(
        """
        INSERT INTO insurance_order_status_history (order_id, from_status, to_status, note, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (order_id, current.value, new_status.value, note, now),
    )
    conn.commit()


def set_period(conn: sqlite3.Connection, order_id: int, *, period_code: str, price_customer_minor: int) -> None:
    """Used by the legacy /o/{token}/period route (pre-existing orders with
    no period yet) and by the post-order /o/{token}/edit-coverage route."""
    conn.execute(
        "UPDATE insurance_orders SET period_code = ?, price_customer_minor = ?, updated_at = ? WHERE id = ?",
        (period_code, price_customer_minor, _now(), order_id),
    )
    conn.commit()


def update_coverage(
    conn: sqlite3.Connection, order_id: int, *, vehicle_category_code: str, period_code: str, price_customer_minor: int
) -> None:
    """Post-order edit of category+period+price (see /o/{token}/edit-coverage).
    end_date depends on start_date, which this doesn't touch -- the caller
    recomputes it and calls set_dates() separately."""
    conn.execute(
        "UPDATE insurance_orders SET vehicle_category_code = ?, period_code = ?, price_customer_minor = ?, updated_at = ? WHERE id = ?",
        (vehicle_category_code, period_code, price_customer_minor, _now(), order_id),
    )
    conn.commit()


def set_dates(conn: sqlite3.Connection, order_id: int, *, start_date: date, end_date: date) -> None:
    """Used by the legacy /o/{token}/date route (pre-existing orders with no
    dates yet), by post-order /o/{token}/edit-date, and by /o/{token}/edit-
    coverage (a period change recomputes end_date for the same start_date)."""
    conn.execute(
        "UPDATE insurance_orders SET start_date = ?, end_date = ?, updated_at = ? WHERE id = ?",
        (start_date.isoformat(), end_date.isoformat(), _now(), order_id),
    )
    conn.commit()


def update_policyholder(conn: sqlite3.Connection, order_id: int, *, full_name: str, contact_type: str, contact_value: str) -> None:
    """Post-order edit of the policyholder's name/contact (see
    /o/{token}/edit-policyholder). Legal-entity policyholders are still not
    supported -- callers only ever pass the individual fields."""
    conn.execute(
        "UPDATE insurance_orders SET full_name = ?, contact_type = ?, contact_value = ?, updated_at = ? WHERE id = ?",
        (full_name, contact_type, contact_value, _now(), order_id),
    )
    conn.commit()


def update_vehicle_fields(
    conn: sqlite3.Connection,
    order_id: int,
    *,
    registration_number: str,
    identifier_type: str,
    identifier: str,
    manufacturer_id: int,
    manufacturer_name: str,
    model_id: int,
    model_name: str,
) -> None:
    """See create_order's docstring re: vehicle_make/vehicle_model as a
    snapshot — editing vehicle data re-snapshots the new manufacturer/model
    name at the time of the edit, same rule as at creation."""
    conn.execute(
        """
        UPDATE insurance_orders
        SET car_number = ?, identifier_type = ?, identifier = ?,
            manufacturer_id = ?, vehicle_make = ?, model_id = ?, vehicle_model = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            registration_number,
            identifier_type,
            identifier,
            manufacturer_id,
            manufacturer_name,
            model_id,
            model_name,
            _now(),
            order_id,
        ),
    )
    conn.commit()
