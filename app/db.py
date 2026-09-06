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

-- One row per Order that has ever started real TPL (Georgia) policy
-- issuance -- see app.integrations.tpl_ge. A separate table rather than
-- more insurance_orders columns: this state is entirely specific to one
-- country's one downstream integration (always NULL/absent for AM/TR and
-- for any GE order that never reaches PAID), and it already needs its own
-- lifecycle (pending -> application_created -> bog_link_ready ->
-- operator_reported_paid, or failed) independent of the order's own status
-- history -- same reasoning that already keeps status history in its own
-- table rather than columns on insurance_orders.
--
-- tpl_uid is the SAME value sent to TPL as "uId" AND reused as
-- "policyUId" for the BOG handoff -- generated exactly once per order and
-- never regenerated (see app.integrations.tpl_ge.service.issue_tpl_policy),
-- which is what guarantees a repeated admin click never creates a second
-- TPL application. tpl_purchase_price_gel is stored as TEXT (an exact
-- decimal string, e.g. "30.00") -- never a float, same money-handling rule
-- as the rest of this project. Never store card/CVC/OTP/3DS/BOG-token data
-- here or anywhere else -- bog_payment_url itself is the one genuinely
-- sensitive value this table holds (see app.integrations.tpl_ge module
-- docstring).
CREATE TABLE IF NOT EXISTS insurance_tpl_issuance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE,
    tpl_uid TEXT NOT NULL UNIQUE,
    tpl_product_id INTEGER,
    tpl_purchase_price_gel TEXT,
    bog_payment_url TEXT,
    issuance_status TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES insurance_orders (id)
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
    # Multi-field contacts (email required, the rest optional) replacing the
    # old single contact_type/contact_value radio-select design. Those two
    # legacy columns are intentionally left in place, unused by new code, so
    # orders created before this migration keep reading back correctly --
    # see app/orders/models.py's Order.contact_rows.
    ("contact_email", "TEXT"),
    ("contact_telegram", "TEXT"),
    ("contact_phone", "TEXT"),
    ("contact_max", "TEXT"),
    ("contact_other", "TEXT"),
    # Policyholder's own identity fields (tpl.ge parity, see /policyholder)
    # -- bare names matching full_name's own naming convention (no
    # "policyholder_" prefix; the Order's primary identity fields ARE the
    # policyholder's by convention already, see full_name/contact_* above).
    # citizenship, when set, is always one of app.countries.COUNTRIES.
    ("identification_number", "TEXT"),
    ("citizenship", "TEXT"),
    # Driver/owner ("Водитель"/"Владелец" — tpl.ge parity, see /policyholder).
    # *_same_as_policyholder default to 1 (true) via the column DEFAULT
    # itself, not just app-code -- so a pre-existing order, which never had
    # a driver/owner concept at all, reads back as "same as policyholder"
    # (the normal, unremarkable case) rather than NULL/false. The *_full_name/
    # identifier/citizenship/phone/email columns stay NULL for such an order,
    # same as any other never-collected optional field.
    ("driver_same_as_policyholder", "INTEGER NOT NULL DEFAULT 1"),
    ("driver_full_name", "TEXT"),
    ("driver_identifier", "TEXT"),
    ("driver_citizenship", "TEXT"),
    ("driver_phone", "TEXT"),
    ("driver_email", "TEXT"),
    ("owner_same_as_policyholder", "INTEGER NOT NULL DEFAULT 1"),
    # "individual" | "legal" -- only meaningful when owner_same_as_policyholder
    # is false; NULL otherwise (see app.validation.validate_owner_form).
    ("owner_entity_type", "TEXT"),
    # For a legal entity owner, this holds the company name and
    # owner_identifier holds its identification code -- same two columns,
    # relabeled per entity_type, rather than a parallel set of company-only
    # columns (see the OCR task report's "OWNER" section for why).
    ("owner_full_name", "TEXT"),
    ("owner_identifier", "TEXT"),
    ("owner_citizenship", "TEXT"),  # individual only; NULL for legal entities
    ("owner_phone", "TEXT"),
    ("owner_email", "TEXT"),
    # Country-specific vehicle/policyholder fields (AM/TR only -- see
    # app.web.checkout_routes) -- all NULL for Georgia and for every order
    # created before this migration. engine_power/model_year are integers
    # (horsepower / calendar year); date_of_birth is stored as an ISO date
    # string, same convention as start_date/end_date (see Order.from_row).
    ("engine_power", "INTEGER"),
    ("model_year", "INTEGER"),
    ("date_of_birth", "TEXT"),
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
    # check_same_thread=False: FastAPI dispatches every sync dependency in a
    # request's chain (app.deps.get_db itself, get_session_id, the sync
    # endpoint function, teardown) via its OWN separate
    # starlette.concurrency.run_in_threadpool -> anyio.to_thread.run_sync
    # call. anyio services each of those from a shared, floating worker-
    # thread pool -- nothing pins a request's dependency chain to one OS
    # thread, so under real concurrent load the connection app.deps.get_db
    # creates in one worker thread routinely gets used from a different one
    # moments later (reproduced deterministically in
    # tests/test_db.py::test_concurrent_requests_do_not_hit_sqlite_cross_thread_error,
    # which fails with sqlite3's default check_same_thread=True and passes
    # with it disabled). This is safe specifically because of how this
    # function is used: app.deps.get_db creates a brand-new Connection per
    # request and never shares it across requests or stores it in any
    # global/cache, so within one request's lifetime the handoffs between
    # threads are strictly sequential (each awaited dispatch completes
    # before the next begins) -- never two threads touching the connection
    # at the same instant. Disabling the same-thread check only removes a
    # guarantee this usage pattern never relied on; it does not make
    # genuinely concurrent access to a shared connection safe, and nothing
    # here introduces that.
    conn = sqlite3.connect(db_path, check_same_thread=False)
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
