"""Phase 2: additive Telegram schema + bot configuration.

Fresh tmp_path SQLite files only; never the shared test DB, never the real
data/insurance.db."""

import logging
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app import db as db_module
from app.dates.rules import today_in_georgia
from app.db import get_connection, init_db
from app.deps import PROJECT_ROOT
from app.orders.models import Order
from app.orders.repository import create_order, get_order_by_id
from app.settings import load_settings
from app.telegram_bot.app import SecretRedactingFilter
from app.telegram_bot.config import BotConfigError, load_bot_config, parse_manager_ids
from app.telegram_bot.storage import ConversationDataNotAllowed, SessionConversationStorage

_FAKE_TOKEN = "123456789:" + "B" * 35
_NEW_ORDER_COLUMNS = ["channel", "bot_key", "telegram_user_id", "telegram_chat_id", "telegram_username", "acquisition_source"]


def _columns(conn, table):
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


# ----------------------------------------------------------------- migrations


def test_init_db_is_idempotent(tmp_path):
    db_file = tmp_path / "idem.db"
    init_db(db_file)
    init_db(db_file)
    init_db(db_file)
    conn = get_connection(db_file)
    try:
        order_columns = _columns(conn, "insurance_orders")
        for column in _NEW_ORDER_COLUMNS:
            assert order_columns.count(column) == 1
        assert _columns(conn, "insurance_sessions").count("conversation_state") == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'insurance_order_files'").fetchone()
    finally:
        conn.close()


def _pre_migration_db(tmp_path, monkeypatch) -> Path:
    """A DB exactly as it looked before this change: no Telegram columns, no
    insurance_order_files table, plus a legacy web order and session."""
    db_file = tmp_path / "legacy.db"
    old_schema = db_module.SCHEMA.split("-- Files attached to an Order by the Telegram bot")[0]
    old_orders = [c for c in db_module._ORDER_COLUMN_MIGRATIONS if c[0] not in _NEW_ORDER_COLUMNS]
    monkeypatch.setattr(db_module, "SCHEMA", old_schema)
    monkeypatch.setattr(
        db_module,
        "_COLUMN_MIGRATIONS",
        {
            **{k: v for k, v in db_module._COLUMN_MIGRATIONS.items() if k not in ("insurance_sessions", "insurance_order_files")},
            "insurance_orders": [c for c in old_orders if c[0] != "client_checkout_id"],
        },
    )
    monkeypatch.setattr(db_module, "_POST_MIGRATION_SQL", [])  # phase-5 indexes didn't exist either
    init_db(db_file)
    conn = get_connection(db_file)
    try:
        assert "channel" not in _columns(conn, "insurance_orders")
        conn.execute(
            """INSERT INTO insurance_orders (public_number, country_code, status, session_id, customer_currency,
                   purchase_currency, resume_token, created_at, updated_at)
               VALUES ('ORDER-LEGACY', 'GE', 'awaiting_payment', 'web-sess', 'RUB', 'GEL', 'tok-legacy',
                   '2026-01-01T00:00:00', '2026-01-01T00:00:00')"""
        )
        conn.execute(
            "INSERT INTO insurance_sessions (session_id, draft_data, created_at, last_seen_at) VALUES ('web-sess', '{}', 'x', 'x')"
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.undo()
    return db_file


def test_existing_db_is_migrated_and_legacy_rows_stay_readable(tmp_path, monkeypatch):
    db_file = _pre_migration_db(tmp_path, monkeypatch)
    init_db(db_file)  # the real, current migrations
    conn = get_connection(db_file)
    try:
        row = conn.execute("SELECT * FROM insurance_orders WHERE public_number = 'ORDER-LEGACY'").fetchone()
        order = Order.from_row(row)
        assert order.channel == "web"  # NOT NULL DEFAULT 'web'
        assert order.bot_key is None
        assert order.telegram_user_id is None
        assert order.telegram_chat_id is None
        assert order.telegram_username is None
        assert order.acquisition_source is None
        session = conn.execute("SELECT conversation_state FROM insurance_sessions WHERE session_id = 'web-sess'").fetchone()
        assert session["conversation_state"] is None
    finally:
        conn.close()


def test_web_create_order_defaults_to_web_channel(tmp_path):
    db_file = tmp_path / "web.db"
    init_db(db_file)
    conn = get_connection(db_file)
    try:
        order = create_order(conn, **_order_kwargs())
        assert order.channel == "web"
        assert order.bot_key is None and order.telegram_user_id is None and order.acquisition_source is None
    finally:
        conn.close()


def test_telegram_metadata_round_trips(tmp_path):
    db_file = tmp_path / "tg.db"
    init_db(db_file)
    conn = get_connection(db_file)
    try:
        order = create_order(
            conn,
            **_order_kwargs(),
            channel="telegram",
            bot_key="testbot",
            telegram_user_id=42,
            telegram_chat_id=42,
            telegram_username="someone",
            acquisition_source="upper_lars",
        )
        reread = get_order_by_id(conn, order.id)
        assert (reread.channel, reread.bot_key, reread.telegram_user_id, reread.telegram_chat_id) == ("telegram", "testbot", 42, 42)
        assert (reread.telegram_username, reread.acquisition_source) == ("someone", "upper_lars")
    finally:
        conn.close()


def _order_kwargs():
    start = today_in_georgia() + timedelta(days=5)
    return dict(
        session_id="s",
        country_code="GE",
        vehicle_category_code="passenger_car",
        period_code="30d",
        start_date=start,
        end_date=start + timedelta(days=30),
        price_customer_minor=214900,
        data_entry_method="manual",
        registration_number="AA123BB",
        identifier_type="vin",
        identifier="JYARJ41E7KA000700",
        manufacturer_id=1,
        manufacturer_name="M",
        model_id=1,
        model_name="X",
        full_name="Ivanov Ivan",
        contact_email="a@example.com",
        contact_telegram=None,
        contact_phone=None,
        contact_max=None,
        contact_other=None,
        customer_currency="RUB",
        purchase_currency="GEL",
    )


def _files_conn(tmp_path):
    db_file = tmp_path / "files.db"
    init_db(db_file)
    conn = get_connection(db_file)
    order = create_order(conn, **_order_kwargs())
    return conn, order


def _insert_file(conn, order_id, kind="payment_receipt", unique_id="uniq-1"):
    conn.execute(
        """INSERT INTO insurance_order_files (order_id, kind, bot_key, telegram_file_id, telegram_file_unique_id, created_at)
           VALUES (?, ?, 'testbot', 'file-id', ?, 'now')""",
        (order_id, kind, unique_id),
    )


def test_order_files_duplicate_upload_is_rejected_by_unique_key(tmp_path):
    conn, order = _files_conn(tmp_path)
    try:
        _insert_file(conn, order.id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_file(conn, order.id)
        _insert_file(conn, order.id, kind="vehicle_document")  # same file, different kind: allowed
    finally:
        conn.close()


def test_order_files_kind_is_constrained(tmp_path):
    conn, order = _files_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_file(conn, order.id, kind="selfie")
    finally:
        conn.close()


# ------------------------------------------------------ conversation storage


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_conversation_storage_persists_state_only(tmp_path):
    from aiogram.fsm.storage.base import StorageKey

    db_file = tmp_path / "fsm.db"
    init_db(db_file)
    key = StorageKey(bot_id=1, chat_id=77, user_id=77)
    storage = SessionConversationStorage(db_file, "testbot")
    _run(storage.set_state(key, "Flow:waiting_start_date"))

    fresh = SessionConversationStorage(db_file, "testbot")  # "restarted" process
    assert _run(fresh.get_state(key)) == "Flow:waiting_start_date"
    assert _run(SessionConversationStorage(db_file, "otherbot").get_state(key)) is None
    assert _run(fresh.get_data(key)) == {}
    _run(fresh.set_data(key, {}))  # FSMContext.clear() does this -- must work
    with pytest.raises(ConversationDataNotAllowed):
        _run(fresh.set_data(key, {"vin": "X"}))


# --------------------------------------------------------------- bot config


@pytest.fixture
def bot_env(monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)  # real config.yaml -> real profiles
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_KEY", "osago24ge")
    monkeypatch.setenv("TELEGRAM_BOT_MANAGER_IDS", "111, 222 333")
    return monkeypatch


def test_valid_bot_config_loads(bot_env):
    config = load_bot_config(load_settings(PROJECT_ROOT))
    assert config.profile.bot_key == "osago24ge"
    assert config.profile.country_code == "GE"
    assert config.manager_ids == frozenset({111, 222, 333})
    assert config.token.get_secret_value() == _FAKE_TOKEN


def test_token_never_appears_in_repr(bot_env):
    settings = load_settings(PROJECT_ROOT)
    config = load_bot_config(settings)
    for rendered in (repr(settings), str(settings), repr(settings.telegram_bot), repr(config), str(config)):
        assert _FAKE_TOKEN not in rendered
        assert _FAKE_TOKEN.split(":")[1] not in rendered


@pytest.mark.parametrize(
    ("var", "value", "message_part"),
    [
        ("TELEGRAM_BOT_TOKEN", None, "TELEGRAM_BOT_TOKEN is not set"),
        ("TELEGRAM_BOT_TOKEN", "not-a-token-" + "C" * 30, "does not look like"),
        ("TELEGRAM_BOT_KEY", None, "TELEGRAM_BOT_KEY is not set"),
        ("TELEGRAM_BOT_KEY", "Bad Key!", "must match"),
        ("TELEGRAM_BOT_KEY", "nosuchbot", "No telegram_bots.nosuchbot profile"),
        ("TELEGRAM_BOT_MANAGER_IDS", None, "TELEGRAM_BOT_MANAGER_IDS is not set"),
        ("TELEGRAM_BOT_MANAGER_IDS", "123,abc", "numeric"),
        ("TELEGRAM_BOT_MANAGER_IDS", "-5", "numeric"),
    ],
)
def test_bot_config_fails_closed_without_echoing_values(bot_env, var, value, message_part):
    if value is None:
        bot_env.delenv(var, raising=False)
    else:
        bot_env.setenv(var, value)
    with pytest.raises(BotConfigError) as excinfo:
        load_bot_config(load_settings(PROJECT_ROOT))
    assert message_part in str(excinfo.value)
    assert _FAKE_TOKEN not in str(excinfo.value)
    if var == "TELEGRAM_BOT_TOKEN" and value:
        assert value not in str(excinfo.value)


def test_malformed_bot_variables_never_break_web_settings(bot_env):
    bot_env.setenv("TELEGRAM_BOT_MANAGER_IDS", "garbage")
    bot_env.setenv("TELEGRAM_BOT_TOKEN", "garbage")
    load_settings(PROJECT_ROOT)  # web app startup path: must not raise


def test_parse_manager_ids_accepts_separators():
    assert parse_manager_ids(" 1,2 ,3\t4 ") == frozenset({1, 2, 3, 4})


def test_secret_redacting_filter_masks_token():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "POST https://api.telegram.org/bot%s/getMe", (_FAKE_TOKEN,), None)
    SecretRedactingFilter(_FAKE_TOKEN).filter(record)
    assert _FAKE_TOKEN not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


# ------------------------------------------------------------ multi-bot hygiene


def test_no_python_code_names_a_concrete_bot():
    """Bot identity lives in config (telegram_bots.*), never in code."""
    pattern = re.compile(r"osago24ge", re.IGNORECASE)
    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for path in (PROJECT_ROOT / "app").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
