"""Raw-SQL repository for the vehicle catalog (categories/manufacturers/models).

Same conventions as app/orders/repository.py and app/sessions/repository.py:
raw parameterized SQL, no ORM, upserts via ON CONFLICT. Sync (app.catalog.sync)
is the only writer for most of these rows; this module is also what routes use
read-only to populate the category/manufacturer/model pickers and to validate
submitted IDs server-side (never trust manufacturer_id/model_id from the browser).
"""

import sqlite3
from datetime import datetime, timezone

from app.catalog.models import Manufacturer, VehicleCategory, VehicleModel


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def list_categories(
    conn: sqlite3.Connection, *, active_only: bool = True, allowed_codes: list[str] | tuple[str, ...] | None = None
) -> list[VehicleCategory]:
    """allowed_codes is the one place country-specific category availability
    is enforced (see app.web.checkout_routes._allowed_category_codes, the
    only caller that ever passes it) -- None means unrestricted (every
    category the catalog has, exactly today's behaviour), which is what
    keeps Georgia's list untouched. An explicit empty list is a real
    "nothing enabled for this country yet" state, not the same as
    unrestricted -- short-circuits before hitting the same empty-IN()
    pitfall documented on deactivate_categories_not_in below."""
    if allowed_codes is not None and not allowed_codes:
        return []

    query = "SELECT * FROM insurance_vehicle_categories"
    conditions = []
    params: list = []
    if active_only:
        conditions.append("active = 1")
    if allowed_codes is not None:
        placeholders = ",".join("?" for _ in allowed_codes)
        conditions.append(f"code IN ({placeholders})")
        params.extend(allowed_codes)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id"
    return [VehicleCategory.from_row(row) for row in conn.execute(query, params).fetchall()]


def get_category_by_code(conn: sqlite3.Connection, code: str) -> VehicleCategory | None:
    row = conn.execute(
        "SELECT * FROM insurance_vehicle_categories WHERE code = ? AND active = 1", (code,)
    ).fetchone()
    return VehicleCategory.from_row(row) if row else None


def upsert_category(
    conn: sqlite3.Connection, *, external_id: int, code: str, name: str, icon: str | None
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO insurance_vehicle_categories (external_id, code, name, icon, active, synced_at)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(external_id) DO UPDATE SET
            code = excluded.code, name = excluded.name, icon = excluded.icon,
            active = 1, synced_at = excluded.synced_at
        """,
        (external_id, code, name, icon, now),
    )


def deactivate_categories_not_in(conn: sqlite3.Connection, external_ids: list[int]) -> int:
    # `x NOT IN ()` has no valid SQL spelling, and `NOT IN (NULL)` is always
    # NULL/unknown (never true) — an empty external_ids must deactivate
    # everything, not (via that NULL quirk) silently deactivate nothing.
    if not external_ids:
        cursor = conn.execute("UPDATE insurance_vehicle_categories SET active = 0")
        return cursor.rowcount
    placeholders = ",".join("?" for _ in external_ids)
    cursor = conn.execute(
        f"UPDATE insurance_vehicle_categories SET active = 0 WHERE external_id NOT IN ({placeholders})",
        external_ids,
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Manufacturers
# ---------------------------------------------------------------------------


def list_manufacturers(conn: sqlite3.Connection, *, active_only: bool = True) -> list[Manufacturer]:
    query = "SELECT * FROM insurance_manufacturers"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY is_popular DESC, name"
    return [Manufacturer.from_row(row) for row in conn.execute(query).fetchall()]


def search_manufacturers(conn: sqlite3.Connection, query_text: str, *, limit: int = 50) -> list[Manufacturer]:
    like = f"%{query_text.strip()}%"
    rows = conn.execute(
        """
        SELECT * FROM insurance_manufacturers
        WHERE active = 1 AND name LIKE ? ESCAPE '\\'
        ORDER BY is_popular DESC, name
        LIMIT ?
        """,
        (like, limit),
    ).fetchall()
    return [Manufacturer.from_row(row) for row in rows]


def get_manufacturer(conn: sqlite3.Connection, manufacturer_id: int) -> Manufacturer | None:
    row = conn.execute(
        "SELECT * FROM insurance_manufacturers WHERE id = ? AND active = 1", (manufacturer_id,)
    ).fetchone()
    return Manufacturer.from_row(row) if row else None


def upsert_manufacturer(
    conn: sqlite3.Connection, *, external_id: int, name: str, is_popular: bool
) -> int:
    now = _now()
    conn.execute(
        """
        INSERT INTO insurance_manufacturers (external_id, name, is_popular, active, synced_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(external_id) DO UPDATE SET
            name = excluded.name, is_popular = excluded.is_popular,
            active = 1, synced_at = excluded.synced_at
        """,
        (external_id, name, int(is_popular), now),
    )
    row = conn.execute(
        "SELECT id FROM insurance_manufacturers WHERE external_id = ?", (external_id,)
    ).fetchone()
    return row["id"]


def mark_models_synced(conn: sqlite3.Connection, manufacturer_id: int, synced_at: str | None = None) -> None:
    """Records that this manufacturer's models have actually been looked at
    — even if the sync produced zero rows, this distinguishes a genuinely
    empty catalog from one we simply haven't checked yet."""
    conn.execute(
        "UPDATE insurance_manufacturers SET models_synced_at = ? WHERE id = ?",
        (synced_at or _now(), manufacturer_id),
    )


def deactivate_manufacturers_not_in(conn: sqlite3.Connection, external_ids: list[int]) -> int:
    # See deactivate_categories_not_in for why the empty-list case needs its
    # own query rather than a `NOT IN (NULL)` placeholder trick.
    if not external_ids:
        cursor = conn.execute("UPDATE insurance_manufacturers SET active = 0")
        return cursor.rowcount
    placeholders = ",".join("?" for _ in external_ids)
    cursor = conn.execute(
        f"UPDATE insurance_manufacturers SET active = 0 WHERE external_id NOT IN ({placeholders})",
        external_ids,
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def list_models(conn: sqlite3.Connection, manufacturer_id: int, *, active_only: bool = True) -> list[VehicleModel]:
    query = "SELECT * FROM insurance_vehicle_models WHERE manufacturer_id = ?"
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY name"
    return [VehicleModel.from_row(row) for row in conn.execute(query, (manufacturer_id,)).fetchall()]


def get_model(conn: sqlite3.Connection, model_id: int) -> VehicleModel | None:
    row = conn.execute(
        "SELECT * FROM insurance_vehicle_models WHERE id = ? AND active = 1", (model_id,)
    ).fetchone()
    return VehicleModel.from_row(row) if row else None


def upsert_model(conn: sqlite3.Connection, *, external_id: int, manufacturer_id: int, name: str) -> int:
    now = _now()
    conn.execute(
        """
        INSERT INTO insurance_vehicle_models (external_id, manufacturer_id, name, active, synced_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(manufacturer_id, external_id) DO UPDATE SET
            name = excluded.name, active = 1, synced_at = excluded.synced_at
        """,
        (external_id, manufacturer_id, name, now),
    )
    row = conn.execute(
        "SELECT id FROM insurance_vehicle_models WHERE manufacturer_id = ? AND external_id = ?",
        (manufacturer_id, external_id),
    ).fetchone()
    return row["id"]


def deactivate_models_not_in(conn: sqlite3.Connection, manufacturer_id: int, external_ids: list[int]) -> int:
    # See deactivate_categories_not_in for why the empty-list case needs its
    # own query rather than a `NOT IN (NULL)` placeholder trick.
    if not external_ids:
        cursor = conn.execute(
            "UPDATE insurance_vehicle_models SET active = 0 WHERE manufacturer_id = ?", (manufacturer_id,)
        )
        return cursor.rowcount
    placeholders = ",".join("?" for _ in external_ids)
    cursor = conn.execute(
        f"UPDATE insurance_vehicle_models SET active = 0 "
        f"WHERE manufacturer_id = ? AND external_id NOT IN ({placeholders})",
        (manufacturer_id, *external_ids),
    )
    return cursor.rowcount
