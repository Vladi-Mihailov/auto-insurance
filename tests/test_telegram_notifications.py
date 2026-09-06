"""Operator Telegram notification on PAYMENT_REVIEW -> PAID.

Covers: exactly-once send on a real confirm, no second send on a repeated
confirm of an already-PAID order, Telegram/Telethon failures never
affecting the payment status or crashing the admin endpoint, message
formatting (core fields, owner/driver "same as policyholder" text,
optional contacts never rendered as empty/"None"), and that the dedicated
auto-insurance session path (never an ai-lead-radar session) is what
actually gets used. No real Telegram/Telethon connection is ever made —
every HTTP-level test monkeypatches app.notifications.telegram.send_message
(or, for the exception-mapping test, the telethon.TelegramClient class
itself with a fake that never touches the network).
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import get_settings
from app.main import app
from app.notifications import telegram as telegram_module
from app.notifications.telegram import TelegramNotifyError, format_paid_order_message, notify_operator_order_paid
from app.orders.models import Order
from app.orders.repository import get_order_by_token
from app.orders.state_machine import OrderStatus
from app.dates.rules import today_in_georgia
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
# external_id range chosen to not collide with other test files' fixtures.
_manufacturer_id = upsert_manufacturer(_conn, external_id=14001, name="ZTGRAMFICTIONALMAKE", is_popular=True)
_model_id = upsert_model(_conn, external_id=14001, manufacturer_id=_manufacturer_id, name="ZTGRAMFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()


def _order(resume_token):
    conn = get_connection(_settings.app.db_file)
    try:
        return get_order_by_token(conn, resume_token)
    finally:
        conn.close()


def _create_order_in_payment_review(client_, plate="TGRAM01"):
    """Drives the real checkout flow up to PAYMENT_REVIEW -- same shape as
    tests/test_payment.py's helper, except the start date is computed at
    run time (today_in_georgia()) rather than a fixed string, since a
    hardcoded past date would fail "date can't be in the past" validation
    regardless of this feature."""
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client_.post("/date", data={"start_date": today_in_georgia().isoformat()})
    client_.post("/method", data={"choice": "manual"})
    client_.post(
        "/vehicle",
        data={
            "registration_number": plate,
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000700",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )
    response = client_.post("/policyholder", data=valid_policyholder_data(), follow_redirects=False)
    resume_token = response.headers["location"].split("/")[2]
    client_.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    client_.post(f"/o/{resume_token}/confirm-payment")
    assert _order(resume_token).status == OrderStatus.PAYMENT_REVIEW.value
    return resume_token


_TEST_SESSION_NAME = "auto_insurance_operator_test"


@pytest.fixture
def admin_and_telegram_configured(monkeypatch, tmp_path):
    """Same cache-clear pattern as test_payment.py's admin_configured
    fixture -- get_settings() is @lru_cache'd. TELEGRAM_OPERATOR_SESSION_PATH
    points at a tmp_path-scoped file distinct from both the project's real
    default (data/sessions/auto_insurance_operator) and anything
    ai-lead-radar uses, so tests can assert the CONFIGURED path is what
    actually gets passed through, and never accidentally touch a real
    session file on disk."""
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


def _confirm(client_, resume_token):
    return client_.post(
        f"/admin/orders/{resume_token}/confirm", auth=("admin", "s3cret-test-only"), follow_redirects=False
    )


def _make_order(**overrides) -> Order:
    values = dict(
        id=1,
        public_number="GE-2026-000482",
        country_code="GE",
        status=OrderStatus.PAID.value,
        session_id="sess-1",
        full_name="Ivanov Ivan",
        identification_number="AB1234567",
        citizenship="Georgia",
        contact_email="ivan@example.com",
        contact_telegram=None,
        contact_phone=None,
        contact_max=None,
        contact_other=None,
        period_code="15d",
        start_date=date(2026, 8, 23),
        end_date=date(2026, 9, 6),
        customer_currency="RUB",
        purchase_currency="GEL",
        price_customer_minor=134900,
        resume_token="tok-1",
        created_at=datetime(2026, 8, 20, 10, 0, 0),
        updated_at=datetime(2026, 8, 22, 10, 0, 0),
        vehicle_category_code="passenger_car",
        manufacturer_id=1,
        model_id=1,
        identifier_type="vin",
        identifier="JYARJ41E7KA000700",
        data_entry_method="manual",
        vehicle_make="BMW",
        vehicle_model="X5",
        vin=None,
        car_number="AB123CD",
        contact_type=None,
        contact_value=None,
        driver_same_as_policyholder=True,
        driver_full_name=None,
        driver_identifier=None,
        driver_citizenship=None,
        driver_phone=None,
        driver_email=None,
        owner_same_as_policyholder=True,
        owner_entity_type=None,
        owner_full_name=None,
        owner_identifier=None,
        owner_citizenship=None,
        owner_phone=None,
        owner_email=None,
        engine_power=None,
        model_year=None,
        date_of_birth=None,
    )
    values.update(overrides)
    return Order(**values)


# --------------------------- 1: exactly one send on a real transition -----------


def test_confirm_payment_sends_telegram_notification_exactly_once(admin_and_telegram_configured, monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: sent.append(kwargs))

    client_ = TestClient(app)
    resume_token = _create_order_in_payment_review(client_)
    # _create_order_in_payment_review already triggers its OWN two
    # notifications (new-order at creation, payment-claimed at
    # confirm-payment -- see tests/test_operator_notifications.py for their
    # own dedicated coverage) -- clear those here so this test's "exactly
    # once" assertion is isolated to the PAID notification it actually
    # exercises (the admin confirm below).
    sent.clear()

    response = _confirm(client_, resume_token)

    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.PAID.value
    assert len(sent) == 1
    assert sent[0]["chat_id"] == -5535243432  # numeric chat id parsed to int, matches service_chat_id
    assert sent[0]["api_id"] == 12345
    assert sent[0]["api_hash"] == "test-api-hash-not-real"


def test_confirm_payment_uses_the_dedicated_auto_insurance_session_path(
    admin_and_telegram_configured, monkeypatch, tmp_path
):
    """The session path passed through must be the one configured for
    auto-insurance specifically -- never a hardcoded/default path that
    could accidentally collide with an ai-lead-radar session."""
    sent = []
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: sent.append(kwargs))

    client_ = TestClient(app)
    resume_token = _create_order_in_payment_review(client_)
    sent.clear()  # isolate to the PAID notification -- see the test above
    _confirm(client_, resume_token)

    assert len(sent) == 1
    session_path = sent[0]["session_path"]
    assert session_path.name == _TEST_SESSION_NAME
    assert str(session_path) == str(tmp_path / _TEST_SESSION_NAME)
    # Sanity: this must not be (or contain) any known ai-lead-radar session name.
    for forbidden in ("reader_live", "reader_sync", "reader_notifier"):
        assert forbidden not in str(session_path)


# --------------------------- 2: no second send on an already-PAID order ---------


def test_reconfirming_already_paid_order_does_not_resend(admin_and_telegram_configured, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_module, "send_message", lambda **kwargs: sent.append(kwargs["text"]))

    client_ = TestClient(app)
    resume_token = _create_order_in_payment_review(client_)
    sent.clear()  # isolate to the PAID notification -- see the test above

    first = _confirm(client_, resume_token)
    second = _confirm(client_, resume_token)

    assert first.status_code == 303
    assert second.status_code == 303  # no crash on the repeat
    assert _order(resume_token).status == OrderStatus.PAID.value
    assert len(sent) == 1  # exactly one send across both requests


# --------------------------- 3: Telegram failure never affects payment ----------


def test_telegram_failure_does_not_roll_back_payment_or_crash(admin_and_telegram_configured, monkeypatch):
    def _boom(**kwargs):
        raise TelegramNotifyError("simulated Telegram outage")

    monkeypatch.setattr(telegram_module, "send_message", _boom)

    client_ = TestClient(app)
    resume_token = _create_order_in_payment_review(client_)

    response = _confirm(client_, resume_token)

    assert response.status_code == 303  # never a 500
    assert response.headers["location"] == "/admin/orders?telegram_notify_failed=1"
    assert _order(resume_token).status == OrderStatus.PAID.value  # payment stands


def test_notify_operator_order_paid_returns_false_and_never_raises_on_api_error(monkeypatch, tmp_path):
    """Unit-level check of the notifier itself, independent of the HTTP
    layer above."""

    def _boom(**kwargs):
        raise TelegramNotifyError("simulated Telegram outage")

    monkeypatch.setattr(telegram_module, "send_message", _boom)

    ok = notify_operator_order_paid(
        api_id=12345,
        api_hash="test-hash",
        phone="+10000000000",
        session_path=tmp_path / _TEST_SESSION_NAME,
        chat_id=-5535243432,
        order=_make_order(),
        category_name="Легковой",
        period_label="15 дней",
    )
    assert ok is False


def test_notify_operator_order_paid_returns_false_when_not_configured(tmp_path):
    ok = notify_operator_order_paid(
        api_id=None,
        api_hash=None,
        phone=None,
        session_path=tmp_path / _TEST_SESSION_NAME,
        chat_id=None,
        order=_make_order(),
        category_name="Легковой",
        period_label="15 дней",
    )
    assert ok is False


def test_notify_operator_order_paid_returns_false_when_partially_configured(tmp_path):
    """All four of api_id/api_hash/phone/chat_id are required -- missing
    even one (e.g. phone, needed for the one-time authorization) must
    still skip cleanly rather than attempting a connect that could never
    have worked."""
    ok = notify_operator_order_paid(
        api_id=12345,
        api_hash="test-hash",
        phone=None,
        session_path=tmp_path / _TEST_SESSION_NAME,
        chat_id=-5535243432,
        order=_make_order(),
        category_name="Легковой",
        period_label="15 дней",
    )
    assert ok is False


def test_send_message_wraps_telethon_exceptions_without_leaking_details(monkeypatch, tmp_path):
    """A raw exception from Telethon itself (connection failure, etc.) must
    be caught and re-raised as TelegramNotifyError -- never propagate the
    original exception (which could reference internal Telethon/MTProto
    details) out of send_message. No real network/session is touched: the
    telethon.TelegramClient class itself is replaced with a fake."""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            raise RuntimeError("simulated connection failure")

        async def disconnect(self):
            pass

    monkeypatch.setattr(telegram_module, "TelegramClient", _FakeClient)

    with pytest.raises(TelegramNotifyError):
        telegram_module.send_message(
            api_id=12345,
            api_hash="test-hash",
            session_path=tmp_path / _TEST_SESSION_NAME,
            chat_id=-5535243432,
            text="test",
        )


# --------------------------- message formatting ----------------------------------


def test_message_contains_core_vehicle_and_policyholder_fields():
    order = _make_order()
    text = format_paid_order_message(order, category_name="Легковой", period_label="15 дней")

    assert order.public_number in text
    assert "Легковой" in text
    assert "15 дней" in text
    assert "23.08.2026" in text and "06.09.2026" in text
    assert "1 349" in text
    assert "BMW" in text
    assert "X5" in text
    assert "AB123CD" in text
    assert "JYARJ41E7KA000700" in text
    assert "Ivanov Ivan" in text
    assert "AB1234567" in text
    assert "Georgia" in text
    assert "ОПЛАЧЕНО" in text


def test_message_owner_same_as_policyholder_shows_short_text():
    order = _make_order(owner_same_as_policyholder=True)
    text = format_paid_order_message(order, category_name="Легковой", period_label="15 дней")

    owner_section = text.split("СОБСТВЕННИК")[1].split("ВОДИТЕЛЬ")[0]
    assert "Совпадает со страхователем" in owner_section


def test_message_owner_different_shows_full_saved_data():
    order = _make_order(
        owner_same_as_policyholder=False,
        owner_entity_type="individual",
        owner_full_name="Petrov Petr",
        owner_identifier="CD7654321",
        owner_citizenship="Armenia",
        owner_phone="+995500000000",
    )
    text = format_paid_order_message(order, category_name="Легковой", period_label="15 дней")

    owner_section = text.split("СОБСТВЕННИК")[1].split("ВОДИТЕЛЬ")[0]
    assert "Совпадает со страхователем" not in owner_section
    assert "Petrov Petr" in owner_section
    assert "CD7654321" in owner_section
    assert "Armenia" in owner_section


def test_message_driver_same_as_policyholder_shows_short_text():
    order = _make_order(driver_same_as_policyholder=True)
    text = format_paid_order_message(order, category_name="Легковой", period_label="15 дней")

    driver_section = text.split("ВОДИТЕЛЬ")[1]
    assert "Совпадает со страхователем" in driver_section


def test_message_driver_different_shows_full_saved_data():
    order = _make_order(
        driver_same_as_policyholder=False,
        driver_full_name="Sidorov Sidor",
        driver_identifier="EF1112223",
        driver_citizenship="Georgia",
    )
    text = format_paid_order_message(order, category_name="Легковой", period_label="15 дней")

    driver_section = text.split("ВОДИТЕЛЬ")[1]
    assert "Совпадает со страхователем" not in driver_section
    assert "Sidorov Sidor" in driver_section
    assert "EF1112223" in driver_section


def test_message_omits_empty_optional_contacts_rather_than_showing_none():
    order = _make_order(
        contact_email="ivan@example.com",
        contact_telegram=None,
        contact_phone=None,
        contact_max=None,
        contact_other=None,
    )
    text = format_paid_order_message(order, category_name="Легковой", period_label="15 дней")

    assert "None" not in text


# --------------------- Step 6: country-specific fields (AM/TR) -------------------


def test_message_ge_does_not_show_engine_power_model_year_or_dob():
    """GE order: engine_power/model_year/date_of_birth are all None by
    default (see _make_order) -- the three new lines must simply never
    appear, and the rest of the existing GE message stays exactly as it
    was (see test_message_contains_core_vehicle_and_policyholder_fields)."""
    order = _make_order()
    text = format_paid_order_message(order, category_name="Легковой", period_label="15 дней")

    assert "Мощность двигателя" not in text
    assert "Год выпуска" not in text
    assert "Дата рождения" not in text
    assert "1 349" in text  # amount still present, untouched
    assert "None" not in text


def test_message_am_period_code_none_derives_duration_from_dates_and_shows_engine_power():
    """AM's passenger_car has no period_code by design (see
    app.pricing.provider.get_duration_range) -- admin_routes passes
    period_label=None for it since there's no period to resolve a label
    for. The formatter must derive "N дней" from start_date/end_date
    itself rather than ever rendering "Период: None" or inventing a fake
    period code."""
    order = _make_order(
        country_code="AM",
        period_code=None,
        start_date=date(2026, 10, 27),
        end_date=date(2026, 11, 6),  # exactly 10 days
        engine_power=180,
        model_year=None,
        date_of_birth=None,
    )
    text = format_paid_order_message(order, category_name="Легковой", period_label=None)

    assert "Период: 10 дней" in text
    assert "27.10.2026" in text and "06.11.2026" in text
    assert "Мощность двигателя: 180 л.с." in text
    assert "Год выпуска" not in text  # AM never has model_year
    assert "Дата рождения" not in text  # AM never has date_of_birth
    assert "None" not in text


def test_message_am_missing_engine_power_does_not_crash_and_is_omitted():
    """Missing optional country-specific fields must never crash the
    formatter -- they're simply omitted, same as any other optional field."""
    order = _make_order(
        country_code="AM",
        period_code=None,
        start_date=date(2026, 10, 27),
        end_date=date(2026, 11, 6),
        engine_power=None,
        model_year=None,
        date_of_birth=None,
    )
    text = format_paid_order_message(order, category_name="Легковой", period_label=None)

    assert "Мощность двигателя" not in text
    assert "Период: 10 дней" in text  # rest of the message still renders fine
    assert "None" not in text


def test_message_tr_shows_engine_power_model_year_and_date_of_birth():
    """TR has a real, fixed period_code -- the formatter must use the
    label already resolved upstream (see app.web.admin_routes) rather than
    deriving one, and must show all three Step 4 fields Turkey requires."""
    order = _make_order(
        country_code="TR",
        period_code="30d",
        engine_power=150,
        model_year=2020,
        date_of_birth=date(1990, 5, 20),
    )
    text = format_paid_order_message(order, category_name="Легковой", period_label="30 дней")

    assert "Период: 30 дней" in text
    assert "Мощность двигателя: 150 л.с." in text
    assert "Год выпуска: 2020" in text
    assert "Дата рождения: 20.05.1990" in text  # dd.mm.yyyy, same convention as "Даты"
    assert "1 349" in text  # amount still rendered
    assert "None" not in text


def test_message_tr_missing_date_of_birth_does_not_crash_and_is_omitted():
    order = _make_order(
        country_code="TR",
        period_code="30d",
        engine_power=150,
        model_year=2020,
        date_of_birth=None,
    )
    text = format_paid_order_message(order, category_name="Легковой", period_label="30 дней")

    assert "Дата рождения" not in text
    assert "Мощность двигателя: 150 л.с." in text  # sibling fields unaffected
    assert "Год выпуска: 2020" in text
    assert "None" not in text
    assert "Email: ivan@example.com" in text
    assert "Телефон:" not in text
    assert "MAX:" not in text
    assert "Другое:" not in text
    assert "Telegram:" not in text
