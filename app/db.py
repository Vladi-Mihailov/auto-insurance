"""SQLite connection + schema for auto-insurance.

Raw SQL, no ORM — same lightweight repository-pattern convention used
elsewhere for this kind of project. Schema is defined as CREATE TABLE IF
NOT EXISTS so re-running init_db() on an existing DB is a no-op.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS insurance_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    draft_data TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insurance_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_number TEXT NOT NULL UNIQUE,
    country_code TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT,
    contact_type TEXT,
    contact_value TEXT,
    vehicle_make TEXT,
    vehicle_model TEXT,
    vin TEXT,
    car_number TEXT,
    full_name TEXT,
    period_code TEXT,
    start_date TEXT,
    end_date TEXT,
    customer_currency TEXT NOT NULL DEFAULT 'RUB',
    purchase_currency TEXT NOT NULL DEFAULT 'GEL',
    price_customer_minor INTEGER,
    resume_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insurance_order_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES insurance_orders (id)
);

CREATE TABLE IF NOT EXISTS insurance_analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    order_id INTEGER,
    event_name TEXT NOT NULL,
    properties TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insurance_vehicle_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id INTEGER NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    icon TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS insurance_manufacturers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_popular INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS insurance_vehicle_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id INTEGER NOT NULL,
    manufacturer_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT,
    UNIQUE (manufacturer_id, external_id),
    FOREIGN KEY (manufacturer_id) REFERENCES insurance_manufacturers (id)
);
"""

# (column, definition) — added to insurance_orders if missing. Idempotent: safe to
# run against a fresh DB (table already has none of these, all get added) or an
# existing dev DB (only missing ones get added). Old columns (vin, vehicle_make,
# vehicle_model) are intentionally left in place, unused by new code, so existing
# rows created under the previous flow keep reading back correctly — see
# app/orders/repository.py and summary.html for the fallback logic.
_ORDER_COLUMN_MIGRATIONS = [
    ("vehicle_category_code", "TEXT"),
    ("manufacturer_id", "INTEGER"),
    ("model_id", "INTEGER"),
    ("identifier_type", "TEXT"),
    ("identifier", "TEXT"),
    ("data_entry_method", "TEXT"),
]

# NULL means "this manufacturer's models have never been synced" — distinct
# from "synced, and there happen to be zero" (see app/catalog/sync.py /
# app/web/checkout_routes.py on-demand sync for why that distinction matters).
_MANUFACTURER_COLUMN_MIGRATIONS = [
    ("models_synced_at", "TEXT"),
]

_COLUMN_MIGRATIONS = {
    "insurance_orders": _ORDER_COLUMN_MIGRATIONS,
    "insurance_manufacturers": _MANUFACTURER_COLUMN_MIGRATIONS,
}


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, migrations in _COLUMN_MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in migrations:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate_columns(conn)
        conn.commit()
    finally:
        conn.close()
