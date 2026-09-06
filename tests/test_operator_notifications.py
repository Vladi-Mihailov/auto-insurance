"""Operator workflow improvements (2026-09-06): NEW ORDER + PAYMENT CLAIMED
Telegram notifications, and the expanded /admin/orders visibility.

Manual payment itself is UNCHANGED -- these are purely additional
notifications plus admin read visibility layered onto the existing state
machine (see app.orders.state_machine). No new Telegram client/service: both
new notifications go through the exact same send_message/Telethon transport
tests/test_telegram_notifications.py already covers for the PAID
notification -- see app.notifications.telegram's module docstring.

Same "no real Telegram/Telethon connection" convention as
tests/test_telegram_notifications.py: every test here monkeypatches
app.notifications.telegram.send_message.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import SESSION_COOKIE_NAME, get_settings
from app.main import app
from app.notifications import telegram as telegram_module
from app.orders.repository import get_order_by_token
from app.orders.state_machine import OrderStatus
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()

# Own catalog rows -- external_id=25001 continues the per-file numbering
# convention.
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=25001, name="ZOPSNOTIFYFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=25001, manufacturer_id=_manufacturer_id, name="ZOPSNOTIFYFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()

_START = today_in_georgia() + timedelta(days=90)
_TEST_SESSION_NAME = "auto_insurance_operator_ops_test"


@pytest.fixture
def telegram_configured(monkeypatch, tmp_path):
    """Same shape as tests/test_telegram_notifications.py's
    admin_and_telegram_configured fixture."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret-test-only")
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-api-hash-not-real")
    monkeypatch.setenv("TELEGRAM_PHONE", "+10000000000")
    monkeypatch.setenv("TELEGRAM_OPERATOR_CHAT_ID", "-5535243432")
    monkeypatch.setenv("TELEGRAM_OPERATOR_SESSION_PATH", str(tmp_path / _TEST_SESSION_NAME))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _order(resume_token):
    conn = get_connection(_settings.app.db_file)
    try:
        return get_order_by_token(conn, resume_token)
    finally:
        conn.close()


def _session_id(client: TestClient) -> str:
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id
    return session_id


def _create_order(client: TestClient, plate: str) -> str:
    """Drives the real checkout flow up to a freshly-created (DATA_COMPLETED)
    order and returns its resume_token -- stops short of summary/payment."""
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client.post("/date", data={"start_date": _START.isoformat()})
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": plate,
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000900",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram=f"@{plate.lower()}"), follow_redirects=False
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


# ---------------------------------------------------------------------------
# 1. New order notification
# ---------------------------------------------------------------------------


def test_order_creation_sends_exactly_one_new_order_notification(telegram_configured, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: sent.append(kwargs))

    client = TestClient(app)
    resume_token = _create_order(client, "NEWORD1")

    assert _order(resume_token).status == OrderStatus.DATA_COMPLETED.value
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "🆕 Новая заявка" in text
    assert _order(resume_token).public_number in text
    assert "Страна: Грузия" in text
    assert "Цена:" in text
    assert "ФИО:" in text
    assert "Контакт:" in text
    # Never the resume_token, anywhere in the message.
    assert resume_token not in text


def test_new_order_notification_failure_does_not_break_order_creation(telegram_configured, monkeypatch):
    def _boom(**kwargs):
        raise telegram_module.TelegramNotifyError("simulated outage")

    monkeypatch.setattr(telegram_module, "send_message", _boom)

    client = TestClient(app)
    resume_token = _create_order(client, "NEWORD2")

    # The order still exists and is DATA_COMPLETED, despite the Telegram
    # failure -- notification failure never blocks/breaks checkout.
    order = _order(resume_token)
    assert order is not None
    assert order.status == OrderStatus.DATA_COMPLETED.value


def test_new_order_notification_not_sent_when_telegram_unconfigured():
    """Default test settings have no Telegram config at all -- order
    creation must succeed exactly as before this feature existed."""
    client = TestClient(app)
    resume_token = _create_order(client, "NEWORD3")
    assert _order(resume_token).status == OrderStatus.DATA_COMPLETED.value


def test_awaiting_payment_does_not_trigger_a_second_new_order_or_payment_notification(telegram_configured, monkeypatch):
    """Reaching AWAITING_PAYMENT (clicking "Перейти к оплате" on summary,
    without yet clicking "Я оплатил") must not send anything beyond the one
    new-order notification already sent at creation."""
    sent = []
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: sent.append(kwargs["text"]))

    client = TestClient(app)
    resume_token = _create_order(client, "NEWORD4")
    assert len(sent) == 1  # new-order only, so far

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    assert _order(resume_token).status == OrderStatus.AWAITING_PAYMENT.value
    assert len(sent) == 1  # still just the new-order notification -- no payment-claimed yet
    assert "💳" not in sent[0]


# ---------------------------------------------------------------------------
# 2. Payment claimed notification
# ---------------------------------------------------------------------------


def test_confirm_payment_sends_exactly_one_payment_claimed_notification(telegram_configured, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: sent.append(kwargs))

    client = TestClient(app)
    resume_token = _create_order(client, "PAYCLAIM1")
    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    sent.clear()  # isolate to the payment-claimed notification

    response = client.post(f"/o/{resume_token}/confirm-payment", follow_redirects=False)

    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.PAYMENT_REVIEW.value
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "💳 Клиент сообщил об оплате" in text
    assert _order(resume_token).public_number in text
    assert "Цена:" in text
    assert "ФИО:" in text
    assert resume_token not in text


def test_repeated_confirm_payment_does_not_duplicate_notification(telegram_configured, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: sent.append(kwargs["text"]))

    client = TestClient(app)
    resume_token = _create_order(client, "PAYCLAIM2")
    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    sent.clear()

    first = client.post(f"/o/{resume_token}/confirm-payment", follow_redirects=False)
    second = client.post(f"/o/{resume_token}/confirm-payment", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303  # no crash on the repeat/refresh
    assert _order(resume_token).status == OrderStatus.PAYMENT_REVIEW.value
    assert len(sent) == 1  # exactly one send across both requests


def test_payment_claimed_notification_failure_does_not_break_confirmation(telegram_configured, monkeypatch):
    def _boom(**kwargs):
        raise telegram_module.TelegramNotifyError("simulated outage")

    monkeypatch.setattr(telegram_module, "send_message", _boom)

    client = TestClient(app)
    resume_token = _create_order(client, "PAYCLAIM3")
    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})

    response = client.post(f"/o/{resume_token}/confirm-payment", follow_redirects=False)

    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.PAYMENT_REVIEW.value  # transition still happened


# ---------------------------------------------------------------------------
# 3. Admin orders: visibility across statuses, actions only where appropriate
# ---------------------------------------------------------------------------


def _admin_get(client: TestClient):
    return client.get("/admin/orders", auth=("admin", "s3cret-test-only"))


def test_admin_orders_shows_all_required_statuses(telegram_configured, monkeypatch):
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: None)
    client = TestClient(app)

    data_completed_token = _create_order(client, "STATUSDC1")

    client2 = TestClient(app)
    awaiting_token = _create_order(client2, "STATUSAP1")
    client2.post(f"/o/{awaiting_token}/summary", data={"action": "pay"})

    client3 = TestClient(app)
    review_token = _create_order(client3, "STATUSPR1")
    client3.post(f"/o/{review_token}/summary", data={"action": "pay"})
    client3.post(f"/o/{review_token}/confirm-payment")

    response = _admin_get(TestClient(app))
    assert response.status_code == 200
    for label in ("Заявка заполнена", "Ожидает оплаты", "Оплата на проверке", "Оплачено", "В обработке", "Полис готов"):
        assert label in response.text
    assert _order(data_completed_token).public_number in response.text
    assert _order(awaiting_token).public_number in response.text
    assert _order(review_token).public_number in response.text


def test_admin_orders_each_row_shows_its_own_status(telegram_configured, monkeypatch):
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: None)
    client = TestClient(app)
    resume_token = _create_order(client, "STATUSROW1")

    response = _admin_get(TestClient(app))
    assert response.status_code == 200
    public_number = _order(resume_token).public_number
    # The order's own card must show "Заявка заполнена" (DATA_COMPLETED),
    # not silently mixed in under a different status's section.
    idx = response.text.find(public_number)
    assert idx != -1
    assert "Заявка заполнена" in response.text[idx : idx + 500]


def test_admin_orders_no_confirm_reject_action_for_data_completed(telegram_configured, monkeypatch):
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: None)
    client = TestClient(app)
    resume_token = _create_order(client, "NOACTION1")

    response = _admin_get(TestClient(app))
    public_number = _order(resume_token).public_number
    idx = response.text.find(public_number)
    # Look only within this order's own card (up to the next card/section).
    card = response.text[idx : idx + 800]
    assert f"/o/{resume_token}/summary" not in response.text  # sanity: not a customer page leak
    assert "Подтвердить оплату" not in card
    assert "Оплата не найдена" not in card


def test_admin_orders_no_confirm_reject_action_for_awaiting_payment(telegram_configured, monkeypatch):
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: None)
    client = TestClient(app)
    resume_token = _create_order(client, "NOACTION2")
    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})

    response = _admin_get(TestClient(app))
    public_number = _order(resume_token).public_number
    idx = response.text.find(public_number)
    card = response.text[idx : idx + 800]
    assert "Подтвердить оплату" not in card
    assert "Оплата не найдена" not in card


def test_admin_orders_confirm_reject_action_present_for_payment_review(telegram_configured, monkeypatch):
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: None)
    client = TestClient(app)
    resume_token = _create_order(client, "ACTIONYES1")
    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    client.post(f"/o/{resume_token}/confirm-payment")

    response = _admin_get(TestClient(app))
    public_number = _order(resume_token).public_number
    idx = response.text.find(public_number)
    card = response.text[idx : idx + 1200]
    assert "Подтвердить оплату" in card
    assert "Оплата не найдена" in card
    assert f"/admin/orders/{resume_token}/confirm" in card
    assert f"/admin/orders/{resume_token}/reject" in card


# ---------------------------------------------------------------------------
# 4. Existing payment confirmation flow still works end-to-end
# ---------------------------------------------------------------------------


def test_existing_admin_confirm_flow_still_transitions_to_paid_and_notifies(telegram_configured, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: sent.append(kwargs))

    client = TestClient(app)
    resume_token = _create_order(client, "STILLWORKS1")
    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    client.post(f"/o/{resume_token}/confirm-payment")
    sent.clear()  # isolate to the admin confirm's own PAID notification

    response = client.post(
        f"/admin/orders/{resume_token}/confirm", auth=("admin", "s3cret-test-only"), follow_redirects=False
    )

    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.PAID.value
    assert len(sent) == 1
    assert "ОПЛАЧЕННЫЙ ЗАКАЗ" in sent[0]["text"]


def test_existing_admin_reject_flow_still_works(telegram_configured, monkeypatch):
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: None)

    client = TestClient(app)
    resume_token = _create_order(client, "STILLWORKS2")
    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    client.post(f"/o/{resume_token}/confirm-payment")

    response = client.post(
        f"/admin/orders/{resume_token}/reject", auth=("admin", "s3cret-test-only"), follow_redirects=False
    )

    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.AWAITING_PAYMENT.value
