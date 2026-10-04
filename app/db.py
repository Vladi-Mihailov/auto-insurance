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

-- Files attached to an Order by the Telegram bot: the customer's vehicle
-- document photos, their payment receipt(s), and the finished policy PDF.
-- Only Telegram's own file references are stored -- NEVER the file bytes
-- (the files stay on Telegram's servers; the bot forwards/re-sends them by
-- file_id). A file_id is only valid for the bot that received it, hence
-- bot_key. telegram_file_unique_id is Telegram's stable per-file identity
-- (same file re-sent = same value), which is what the UNIQUE constraint
-- uses to make a duplicate receipt/document upload a no-op rather than a
-- second row. file_id/file_unique_id are never logged anywhere.
CREATE TABLE IF NOT EXISTS insurance_order_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('vehicle_document', 'payment_receipt', 'policy')),
    bot_key TEXT NOT NULL,
    telegram_file_id TEXT NOT NULL,
    telegram_file_unique_id TEXT NOT NULL,
    mime_type TEXT,
    file_size INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (order_id, kind, telegram_file_unique_id),
    FOREIGN KEY (order_id) REFERENCES insurance_orders (id)
);

-- Admin-edited retail prices (/admin/prices). A row here OVERRIDES the
-- config/config.yaml price for exactly one (country, category, period);
-- no row = the config value applies, unchanged. Read by
-- app.pricing.provider.available_periods, so every consumer (web checkout,
-- Telegram bot, admin) sees the same effective price and none of them
-- knows where it came from. period_code matches config.yaml's own period
-- codes ("15d"/"30d"/"90d") -- an override can only re-price a period the
-- config already defines, never invent one. price_rub is whole rubles,
-- same unit as config.yaml's price_rub. Orders are unaffected: each order
-- stores its own price_customer_minor at creation.
CREATE TABLE IF NOT EXISTS insurance_price_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_code TEXT NOT NULL,
    vehicle_category_code TEXT NOT NULL,
    period_code TEXT NOT NULL,
    price_rub INTEGER NOT NULL CHECK (price_rub > 0),
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    UNIQUE (country_code, vehicle_category_code, period_code)
);

-- One row per (order, manager, purpose): the Bot API message a manager was
-- sent for an order (purpose 'order_card' = the actionable order card), so
-- every manager's card can be edited when the order's state changes, and a
-- repeated notification never sends an uncontrolled second card. A new
-- payment-review round replaces message_id (the previous card has already
-- been edited to its final state by then).
CREATE TABLE IF NOT EXISTS telegram_manager_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    manager_user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (order_id, manager_user_id, purpose),
    FOREIGN KEY (order_id) REFERENCES insurance_orders (id)
);

-- A manager's pending "📄 Отправить полис" action: the NEXT PDF this
-- manager sends the bot is the policy for exactly this order_id (always
-- re-validated against the DB when the file arrives), until it expires.
-- One per (bot, manager). Persisted so a bot restart can't silently
-- attach a PDF to the wrong order or lose the manager's intent.
CREATE TABLE IF NOT EXISTS telegram_manager_upload_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_key TEXT NOT NULL,
    manager_user_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (bot_key, manager_user_id),
    FOREIGN KEY (order_id) REFERENCES insurance_orders (id)
);

-- A manager's pending "💰 Цены" price edit: the NEXT plain-text message this
-- manager sends is the new RUB price for exactly this (country, category,
-- period), until it expires. One per (bot, manager) -- same shape/lifecycle
-- as telegram_manager_upload_contexts above, for the same reason (a bot
-- restart must not lose or misattribute the manager's pending intent). The
-- actual price write still goes through insurance_price_overrides via an
-- explicit confirmation step (app.telegram_bot.staff_prices) -- this table
-- only remembers WHICH cell a forthcoming typed number refers to, never a
-- price value itself.
CREATE TABLE IF NOT EXISTS telegram_manager_price_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_key TEXT NOT NULL,
    manager_user_id INTEGER NOT NULL,
    country_code TEXT NOT NULL,
    vehicle_category_code TEXT NOT NULL,
    period_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (bot_key, manager_user_id)
);

-- The bot's staff: who may use the manager functions (role 'manager') and
-- who may also add/remove managers (role 'owner'). Identity is the
-- Telegram user id; username is display metadata only (refreshed when the
-- person uses the bot). Removal is a soft deactivation (active = 0).
-- source: 'config' = bootstrapped from TELEGRAM_BOT_MANAGER_IDS /
-- TELEGRAM_BOT_OWNER_ID (app.telegram_bot.staff.bootstrap), 'invite' =
-- joined through an owner's one-time invite link.
CREATE TABLE IF NOT EXISTS telegram_bot_staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_key TEXT NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    username TEXT,
    role TEXT NOT NULL CHECK (role IN ('owner', 'manager')),
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (bot_key, telegram_user_id)
);

-- One-time manager invites (deep link t.me/<bot>?start=mgr_<token>). Only
-- the SHA-256 of the token is stored; consuming one is a single
-- compare-and-set UPDATE, so it can never be used twice. An invite only
-- ever grants the 'manager' role.
CREATE TABLE IF NOT EXISTS telegram_bot_staff_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_key TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    consumed_by INTEGER
);

-- Durable outbox of Telegram sends for Telegram-channel orders (manager
-- cards/receipts, customer payment notices, card updates). Whoever changes
-- an order's state (the bot, or the web admin) writes the job in the SAME
-- transaction; only the bot process ever talks to Telegram, draining this
-- table and retrying failures with backoff. dedupe_key makes enqueueing
-- idempotent (INSERT OR IGNORE), so a replayed action never re-sends.
-- payload holds only non-personal routing data (file row ids, message ids).
CREATE TABLE IF NOT EXISTS telegram_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_key TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    order_id INTEGER NOT NULL,
    target_chat_id INTEGER NOT NULL,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (bot_key, dedupe_key),
    FOREIGN KEY (order_id) REFERENCES insurance_orders (id)
);
"""

# Run after the column migrations (they may reference migrated columns).
# Idempotent: IF NOT EXISTS.
_POST_MIGRATION_SQL = [
    # Idempotency key for "create the order for THIS completed checkout":
    # a replayed/doubled create resolves to the existing order instead of a
    # second one. Partial index: every pre-existing/web order has NULL.
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_insurance_orders_client_checkout_id
       ON insurance_orders (client_checkout_id) WHERE client_checkout_id IS NOT NULL""",
    "CREATE INDEX IF NOT EXISTS ix_insurance_orders_telegram_user ON insurance_orders (bot_key, telegram_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_telegram_outbox_pending ON telegram_outbox (bot_key, status, next_attempt_at)",
]

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
    # Which transport created the order. NOT NULL DEFAULT 'web': every
    # pre-existing row (all created by the web checkout) reads back as
    # exactly that, never NULL/unknown.
    ("channel", "TEXT NOT NULL DEFAULT 'web'"),
    # Telegram-channel orders only (NULL for web): which bot identity took
    # the order (see app.telegram_bot.profile.BotProfile.bot_key) and the
    # customer's Telegram identity, so the SAME bot can later notify/deliver
    # the policy to the right chat. telegram_username is display-only (it
    # can change); telegram_user_id is the stable identity.
    ("bot_key", "TEXT"),
    ("telegram_user_id", "INTEGER"),
    ("telegram_chat_id", "INTEGER"),
    ("telegram_username", "TEXT"),
    # Deep-link /start payload (e.g. "upper_lars"), already validated
    # against [A-Za-z0-9_-]{1,64}. NULL = no attributed source.
    ("acquisition_source", "TEXT"),
    # Telegram checkout idempotency key (see _POST_MIGRATION_SQL's unique
    # index). NULL for every web order.
    ("client_checkout_id", "TEXT"),
    # The brand/model text as written in the customer's document (or typed
    # by them) when it is NOT in our catalog and the catalog selection fell
    # back to tpl.ge's own "Other" entry. vehicle_make/vehicle_model stay the
    # catalog-name snapshot (what TPL receives, i.e. "Other"); these keep the
    # real "HAVAL" / "H9 ..." for people (manager card). NULL for every
    # catalog-matched order and every order created before this column.
    ("vehicle_make_document", "TEXT"),
    ("vehicle_model_document", "TEXT"),
    # How the customer's payment is handled. NULL = the normal customer
    # route (payment details -> receipt -> manager check) -- every order
    # created before this column. 'operator' = a manager/owner issued the
    # policy directly for a customer from the bot; payment collection was
    # handled outside the bot and NO receipt/payment check happened (the
    # status history note says so too). See app.orders.payment.
    ("payment_mode", "TEXT"),
    # The Telegram user who created the order (the actor). For a customer's
    # own order the same as telegram_user_id; for an operator order the
    # manager -- never the policyholder, whose identity is only the
    # policyholder fields (full_name, identification_number, citizenship).
    ("created_by_telegram_user_id", "INTEGER"),
]

# NULL means "this manufacturer's models have never been synced" — distinct
# from "synced, and there happen to be zero" (see app/catalog/sync.py /
# app/web/checkout_routes.py on-demand sync for why that distinction matters).
_MANUFACTURER_COLUMN_MIGRATIONS = [
    ("models_synced_at", "TEXT"),
]

# Post-payment policy retrieval (GET /api/policies/{o.id} and .../documents --
# see app.integrations.tpl_ge.service.retrieve_issued_policy). tpl_o_id is
# the TPL-server-issued identifier the BOG handoff mints (confirmed, via real
# HAR evidence, DISTINCT from tpl_uid) -- it previously existed only embedded
# inside bog_payment_url's own query string, never as its own column; this
# migration is what gives it one, extracted once per successful BOG handoff
# (see service._refresh_bog_link) so retrieval never has to re-parse a URL.
# tpl_policy_id is TPL's own internal numeric policy id (distinct from both
# tpl_uid and tpl_o_id, and from the human-facing policy_number). The three
# *_document_url columns are classified straight from TPL's own response
# (see service._classify_documents) -- never constructed from a guessed URL
# pattern. policy_retrieved_at is the "have we successfully retrieved this
# already" marker a repeat call checks before ever calling TPL again (see
# service.retrieve_issued_policy's idempotency guard) -- NULL means "not yet
# retrieved", exactly like insurance_manufacturers.models_synced_at above.
_TPL_ISSUANCE_COLUMN_MIGRATIONS = [
    ("tpl_o_id", "TEXT"),
    ("policy_number", "TEXT"),
    ("tpl_policy_id", "INTEGER"),
    ("policy_document_url", "TEXT"),
    ("invoice_document_url", "TEXT"),
    ("additional_terms_document_url", "TEXT"),
    ("policy_retrieved_at", "TEXT"),
    # Set only once notify_operator_policy_ready (app.notifications.telegram)
    # actually confirms the send -- the idempotency guard that keeps a
    # repeat "Оплата TPL завершена"/"Получить полис повторно" click from
    # ever delivering the same PDF to Telegram twice, while still allowing
    # exactly one resend if this stayed NULL because the first attempt
    # failed (see app.web.admin_routes' delivery helper, the only writer).
    ("policy_sent_to_operator_at", "TEXT"),
    # When the single POST /api/policies was claimed (issuance_status
    # 'application_requested', see app.integrations.tpl_ge.models).
    ("application_requested_at", "TEXT"),
]

# insurance_order_files: how Telegram delivered the file ('photo' or
# 'document') -- decides whether it is re-sent with sendPhoto or
# sendDocument. NULL for rows written before this column existed.
_ORDER_FILE_COLUMN_MIGRATIONS = [
    ("telegram_media_type", "TEXT"),
]

# Telegram bot conversation NAVIGATION state (which question the bot is
# waiting for an answer to, e.g. a typed start date) -- kept on the same
# row as the draft it belongs to, so there is exactly one place per
# customer, and it survives a bot restart. Never business data: that stays
# in draft_data (see app.telegram_bot.storage, which enforces this). NULL
# for every web session.
_SESSION_COLUMN_MIGRATIONS = [
    ("conversation_state", "TEXT"),
]

_COLUMN_MIGRATIONS = {
    "insurance_sessions": _SESSION_COLUMN_MIGRATIONS,
    "insurance_orders": _ORDER_COLUMN_MIGRATIONS,
    "insurance_manufacturers": _MANUFACTURER_COLUMN_MIGRATIONS,
    "insurance_tpl_issuance": _TPL_ISSUANCE_COLUMN_MIGRATIONS,
    "insurance_order_files": _ORDER_FILE_COLUMN_MIGRATIONS,
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
        for statement in _POST_MIGRATION_SQL:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
