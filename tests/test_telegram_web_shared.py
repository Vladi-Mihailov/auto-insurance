"""Phase 5, web <-> Telegram: the web admin and the bot share one payment
service; a Telegram order confirmed/rejected in /admin/orders notifies the
customer through the bot's outbox (never Telethon), and web orders keep
their Telethon operator notifications exactly as before."""

import asyncio
import sqlite3
from datetime import timedelta

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection, init_db
from app.deps import get_settings
from app.main import app
from app.notifications import bot_outbox
from app.orders.payment import confirm_payment, reject_payment, submit_payment_claim
from app.orders.repository import create_order, get_order_by_id, set_status
from app.orders.state_machine import OrderStatus
from app.telegram_bot.outbox import OutboxWorker
from app.web import admin_routes, routes
from telegram_bot_helpers import FAKE_TOKEN, TEST_PROFILE, RecordingSession

_AUTH = ("admin", "s3cret-test-only")

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=18001, name="ZSHAREDFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=18001, manufacturer_id=_manufacturer_id, name="ZSHAREDFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret-test-only")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture
def telethon_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(admin_routes, "notify_operator_order_paid", lambda **kw: calls.append("paid") or True)
    monkeypatch.setattr(routes, "notify_operator_payment_claimed", lambda **kw: calls.append("claimed") or True)
    return calls


def _order(channel="telegram", status=OrderStatus.PAYMENT_REVIEW, chat_id=4242):
    conn = get_connection(get_settings().app.db_file)
    try:
        start = today_in_georgia() + timedelta(days=5)
        kwargs = dict(
            session_id=f"shared-{channel}-{chat_id}",
            country_code="GE",
            vehicle_category_code="passenger_car",
            period_code="30d",
            start_date=start,
            end_date=start + timedelta(days=30),
            price_customer_minor=250000,
            data_entry_method="manual",
            registration_number="SHR001",
            identifier_type="vin",
            identifier="JYARJ41E7KA000900",
            manufacturer_id=_manufacturer_id,
            manufacturer_name="M",
            model_id=_model_id,
            model_name="X",
            full_name="Ivanov Ivan",
            contact_email="a@example.com",
            contact_telegram=None,
            contact_phone="+79001234567",
            contact_max=None,
            contact_other=None,
            customer_currency="RUB",
            purchase_currency="GEL",
        )
        if channel == "telegram":
            kwargs.update(channel="telegram", bot_key=TEST_PROFILE.bot_key, telegram_user_id=chat_id, telegram_chat_id=chat_id)
        order = create_order(conn, **kwargs)
        path = [OrderStatus.AWAITING_PAYMENT, OrderStatus.PAYMENT_REVIEW]
        for step in path[: path.index(status) + 1] if status in path else []:
            set_status(conn, order.id, step)
        return get_order_by_id(conn, order.id)
    finally:
        conn.close()


def _jobs(order_id):
    conn = get_connection(get_settings().app.db_file)
    try:
        return bot_outbox.list_jobs(conn, order_id)
    finally:
        conn.close()


def _status(order_id):
    conn = get_connection(get_settings().app.db_file)
    try:
        return get_order_by_id(conn, order_id).status
    finally:
        conn.close()


def test_web_admin_confirm_of_telegram_order_uses_shared_service_and_bot_outbox(admin, telethon_calls):
    order = _order(chat_id=4242)
    response = admin.post(f"/admin/orders/{order.resume_token}/confirm", auth=_AUTH, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/orders"
    assert _status(order.id) == OrderStatus.PAID.value
    assert telethon_calls == []  # no Telethon operator message for a Telegram order
    (job,) = _jobs(order.id)
    assert (job.kind, job.target_chat_id, job.status) == (bot_outbox.KIND_CUSTOMER_PAYMENT_CONFIRMED, 4242, "pending")

    # The bot process later delivers it -- to the order's own chat.
    session = RecordingSession()
    bot = Bot(token=FAKE_TOKEN, session=session)
    worker = OutboxWorker(get_settings(), TEST_PROFILE)
    conn = get_connection(get_settings().app.db_file)
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(worker.process(bot, conn, job_ids=[job.id])) == 1
    finally:
        loop.close()
        conn.close()
    (message,) = [c for c in session.calls if isinstance(c, SendMessage)]
    assert message.chat_id == 4242 and message.text.startswith("✅ Оплата подтверждена")
    assert _jobs(order.id)[0].status == "sent"


def test_web_admin_reject_of_telegram_order_enqueues_customer_notice(admin, telethon_calls):
    order = _order(chat_id=4243)
    admin.post(f"/admin/orders/{order.resume_token}/reject", auth=_AUTH)
    assert _status(order.id) == OrderStatus.AWAITING_PAYMENT.value
    assert [j.kind for j in _jobs(order.id)] == [bot_outbox.KIND_CUSTOMER_PAYMENT_REJECTED]


def test_web_admin_confirm_of_web_order_still_notifies_telethon(admin, telethon_calls):
    order = _order(channel="web")
    admin.post(f"/admin/orders/{order.resume_token}/confirm", auth=_AUTH)
    assert _status(order.id) == OrderStatus.PAID.value
    assert telethon_calls == ["paid"]
    assert _jobs(order.id) == []


def test_web_payment_claim_skips_telethon_for_telegram_orders(telethon_calls):
    telegram_order = _order(status=OrderStatus.AWAITING_PAYMENT, chat_id=4244)
    web_order = _order(channel="web", status=OrderStatus.AWAITING_PAYMENT)
    client = TestClient(app)
    client.post(f"/o/{telegram_order.resume_token}/confirm-payment")
    client.post(f"/o/{web_order.resume_token}/confirm-payment")
    assert _status(telegram_order.id) == _status(web_order.id) == OrderStatus.PAYMENT_REVIEW.value
    assert telethon_calls == ["claimed"]  # the web order only


def test_payment_service_is_compare_and_set():
    order = _order(chat_id=4245)
    conn = get_connection(get_settings().app.db_file)
    try:
        first = confirm_payment(conn, order.id, actor="web admin")
        second = confirm_payment(conn, order.id, actor="telegram manager 1")
        late_reject = reject_payment(conn, order.id, actor="telegram manager 2")
        claim = submit_payment_claim(conn, order.id, note="late receipt")
        history = conn.execute(
            "SELECT COUNT(*) FROM insurance_order_status_history WHERE order_id = ? AND to_status = 'paid'", (order.id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert first.changed and not second.changed and not late_reject.changed and not claim.changed
    assert second.order.status == OrderStatus.PAID.value
    assert history == 1
    assert len(_jobs(order.id)) == 1  # only the real transition enqueued anything


def test_phase5_migrations_are_idempotent_and_enforce_one_order_per_checkout(tmp_path):
    db_file = tmp_path / "p5.db"
    for _ in range(3):
        init_db(db_file)
    conn = get_connection(db_file)
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
        for name in ("telegram_manager_messages", "telegram_manager_upload_contexts", "telegram_outbox",
                     "ux_insurance_orders_client_checkout_id"):
            assert name in tables
        assert "client_checkout_id" in [r["name"] for r in conn.execute("PRAGMA table_info(insurance_orders)")]
        assert "telegram_media_type" in [r["name"] for r in conn.execute("PRAGMA table_info(insurance_order_files)")]
        conn.execute(
            """INSERT INTO insurance_orders (public_number, country_code, status, resume_token, created_at, updated_at, client_checkout_id)
               VALUES ('A', 'GE', 'draft', 't1', 'x', 'x', 'same')"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO insurance_orders (public_number, country_code, status, resume_token, created_at, updated_at, client_checkout_id)
                   VALUES ('B', 'GE', 'draft', 't2', 'x', 'x', 'same')"""
            )
        # many NULL keys (every web order) are fine
        for token in ("t3", "t4"):
            conn.execute(
                "INSERT INTO insurance_orders (public_number, country_code, status, resume_token, created_at, updated_at) VALUES (?, 'GE', 'draft', ?, 'x', 'x')",
                (token, token),
            )
    finally:
        conn.close()


def test_outbox_enqueue_is_idempotent():
    order = _order(chat_id=4246)
    conn = get_connection(get_settings().app.db_file)
    try:
        first = bot_outbox.enqueue_customer_notice(conn, order, kind=bot_outbox.KIND_CUSTOMER_PAYMENT_CONFIRMED, history_id=1)
        again = bot_outbox.enqueue_customer_notice(conn, order, kind=bot_outbox.KIND_CUSTOMER_PAYMENT_CONFIRMED, history_id=1)
        conn.commit()
    finally:
        conn.close()
    assert first is not None and again is None
