"""A bot profile with fixed contacts (customer_email / customer_phone in
config.yaml telegram_bots.<key>) never asks its customers for email or
phone: the configured values go onto the draft and into the normal order
fields tpl.ge receives. Other profiles, and the web checkout, still ask."""

import dataclasses
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from aiogram.methods import EditMessageText, SendMessage
from fastapi.testclient import TestClient
from PIL import Image

from app.countries import COUNTRIES
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.integrations.tpl_ge.models import LiveProduct
from app.integrations.tpl_ge.service import build_application_payload
from app.ocr.models import OcrResult
from app.ocr.provider import OcrProvider
from app.orders.repository import create_order, get_order_by_id, list_telegram_orders
from app.sessions.repository import get_draft
from app.settings import load_settings
from app.telegram_bot.config import BotConfigError, load_bot_config
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, nav, seed_catalog

EMAIL, PHONE = "tplgee@mail.ru", "+995 574 22 06 25"
FIXED = dataclasses.replace(TEST_PROFILE, customer_email=EMAIL, customer_phone=PHONE)
OTHER_BOT = dataclasses.replace(TEST_PROFILE, bot_key="otherbot", customer_email="desk@example.ge", customer_phone="+995 555 00 00 00")
VIN = "WVWZZZ1JZXW000001"
RESULT = OcrResult(
    provider="fake", registration_number="AB123CD", vin=VIN, chassis_number=None, manufacturer="Toyota", model="Camry",
    policyholder_full_name="PETROV PETR", passport_number="751234567", citizenship="Russian Federation",
)


class Provider(OcrProvider):
    def recognize(self, images):
        return RESULT


def _photo() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1000, 700), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", "+7 900 000-00-00")
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", "Тестов Т.")
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


def _harness(settings, profile=FIXED):
    return BotHarness(settings, profile=profile, manager_ids={999}, ocr_provider=Provider())


def _draft(settings, profile, user_id):
    conn = get_connection(settings.app.db_file)
    try:
        return get_draft(conn, session_id_for(profile.bot_key, user_id)) or {}
    finally:
        conn.close()


def _texts(calls):
    return [c.text for c in calls if isinstance(c, (SendMessage, EditMessageText))]


def _asked_for_contacts(calls) -> bool:
    return any(t.startswith(("✉️ Email", "📱 Телефон")) for t in _texts(calls))


def _manual_to_final(h, user, ids):
    calls = []
    for step in (
        lambda: h.send_text(user, "/start"),
        lambda: h.press(user, "p:passenger_car:30d"),
        lambda: h.press(user, "d:tomorrow"),
        lambda: h.press(user, "e:manual"),
        lambda: h.send_text(user, "AB123CD"),
        lambda: h.send_text(user, VIN),
        lambda: h.press(user, f"mf:{ids['TOYOTA']}"),
        lambda: h.press(user, f"md:{ids['TOYOTA/CAMRY']}"),
        lambda: h.press(user, "vc"),
        lambda: h.send_text(user, "Ivanov Ivan"),
        lambda: h.send_text(user, "AB1234567"),
        lambda: h.press(user, f"cz:{COUNTRIES.index('Russia')}"),
    ):
        calls += step()
    return calls


def _orders(settings, profile, user_id):
    conn = get_connection(settings.app.db_file)
    try:
        return list_telegram_orders(conn, bot_key=profile.bot_key, telegram_user_id=user_id)
    finally:
        conn.close()


# ------------------------------------------------------------------ config


def test_the_real_bot_profile_has_the_fixed_contacts(monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("TELEGRAM_BOT_KEY", "osago24ge")
    monkeypatch.setenv("TELEGRAM_BOT_MANAGER_IDS", "1")
    profile = load_bot_config(load_settings(PROJECT_ROOT)).profile
    assert (profile.customer_email, profile.customer_phone) == (EMAIL, PHONE)  # kept exactly as configured
    assert profile.fixed_contacts() == {"contact_email": EMAIL, "contact_phone": PHONE}


@pytest.mark.parametrize(("field", "value"), [("customer_email", "not-an-email"), ("customer_phone", "call me")])
def test_invalid_fixed_contact_fails_bot_startup(monkeypatch, field, value):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("TELEGRAM_BOT_KEY", "osago24ge")
    monkeypatch.setenv("TELEGRAM_BOT_MANAGER_IDS", "1")
    settings = load_settings(PROJECT_ROOT)
    profile = settings.telegram_bot_profiles["osago24ge"].model_copy(update={field: value})
    settings = settings.model_copy(update={"telegram_bot_profiles": {"osago24ge": profile}})
    with pytest.raises(BotConfigError, match=field):
        load_bot_config(settings)


# ------------------------------------------------------------------- flows


def test_draft_gets_the_configured_contacts(settings, ids):
    h = _harness(settings)
    try:
        h.send_text(make_user(1), "/start")
        draft = _draft(settings, FIXED, 1)
        assert (draft["contact_email"], draft["contact_phone"]) == (EMAIL, PHONE)
    finally:
        h.close()


def test_documents_flow_goes_from_review_straight_to_payment(settings, ids):
    h = _harness(settings)
    try:
        user = make_user(2)
        h.send_text(user, "/start")
        h.press(user, "p:passenger_car:30d")
        h.press(user, "d:tomorrow")
        h.press(user, "e:documents")
        review = last_screen(h.send_album(user, "grp", [("photo", f"f{i}", _photo()) for i in range(3)]))
        assert "Email" not in review.text and "Телефон" not in review.text
        assert not any("Email" in t or "Телефон" in t for t, _ in buttons(review))
        assert buttons(review)[0] == ("✅ Всё верно", "fc:confirm")
        calls = h.press(user, "fc:confirm")  # "✅ Всё верно" -- name/passport/citizenship came from the passport photo
        payment = last_screen(calls)
        (order,) = _orders(settings, FIXED, 2)
        assert payment.text.startswith(f"💳 Оплата заказа {order.public_number}")
        assert not any(t.startswith(("Проверьте заявку", "Проверьте данные")) for t in _texts(calls))
        assert not _asked_for_contacts(calls)
        assert (order.contact_email, order.contact_phone) == (EMAIL, PHONE)
    finally:
        h.close()


def test_manual_flow_never_asks_email_or_phone(settings, ids):
    h = _harness(settings)
    try:
        calls = _manual_to_final(h, make_user(3), ids)
        review = last_screen(calls)
        assert review.text.startswith("Проверьте данные:") and "Email" not in review.text and "Телефон" not in review.text
        calls = h.press(make_user(3), "fc:confirm")
        assert last_screen(calls).text.startswith("💳 Оплата заказа")
        assert not _asked_for_contacts(calls)
    finally:
        h.close()


@pytest.mark.parametrize("target", ["n:email:", "n:phone:", "n:email:final_review", "n:phone:checkout_review"])
def test_contact_steps_cannot_be_reached_even_with_a_crafted_button(settings, ids, target):
    h = _harness(settings)
    try:
        user = make_user(4)
        _manual_to_final(h, user, ids)
        calls = h.press(user, target)
        assert not _asked_for_contacts(calls)
    finally:
        h.close()


def test_order_and_tpl_payload_carry_the_configured_contacts(settings, ids):
    h = _harness(settings)
    try:
        user = make_user(5)
        _manual_to_final(h, user, ids)
        h.press(user, "fc:continue")
        (order,) = _orders(settings, FIXED, 5)
        assert (order.contact_email, order.contact_phone) == (EMAIL, PHONE)
        product = LiveProduct(product_id=1, period=30, period_type="D", price_gel=Decimal("30.00"),
                              min_date=date.today(), max_date=date.today() + timedelta(days=90))
        payload = build_application_payload(
            order, uid="u", product=product, category_external_id=7, manufacturer_external_id=1, model_external_id=1,
            insurer_citizenship_id=1, owner_citizenship_id=1, driver_citizenship_id=1, visitor_id="v",
        )
        for key in ("insurerEmail", "vehicleOwnerEmail", "vehicleDriverEmail"):
            assert payload[key] == EMAIL
        for key in ("insurerPhone", "vehicleOwnerPhone", "vehicleDriverPhone"):
            assert payload[key] == PHONE
    finally:
        h.close()


def test_another_profile_uses_its_own_contacts(settings, ids):
    h = _harness(settings, profile=OTHER_BOT)
    try:
        user = make_user(6)
        _manual_to_final(h, user, ids)
        h.press(user, "fc:continue")
        (order,) = _orders(settings, OTHER_BOT, 6)
        assert (order.contact_email, order.contact_phone) == ("desk@example.ge", "+995 555 00 00 00")
    finally:
        h.close()


def test_a_profile_without_fixed_contacts_still_asks(settings, ids):
    h = _harness(settings, profile=TEST_PROFILE)
    try:
        calls = _manual_to_final(h, make_user(7), ids)
        assert last_screen(calls).text.startswith("✉️ Email")
    finally:
        h.close()


def test_existing_orders_are_not_rewritten(settings, ids):
    conn = get_connection(settings.app.db_file)
    try:
        start = date.today() + timedelta(days=5)
        before = create_order(
            conn, session_id="old", country_code="GE", vehicle_category_code="passenger_car", period_code="30d",
            start_date=start, end_date=start + timedelta(days=30), price_customer_minor=214900, data_entry_method="manual",
            registration_number="OLD1", identifier_type="vin", identifier="JYARJ41E7KA000701", manufacturer_id=ids["TOYOTA"],
            manufacturer_name="TOYOTA", model_id=ids["TOYOTA/CAMRY"], model_name="CAMRY", full_name="Old Customer",
            contact_email="old@example.com", contact_telegram=None, contact_phone="+7 999 111-22-33", contact_max=None,
            contact_other=None, customer_currency="RUB", purchase_currency="GEL",
            channel="telegram", bot_key=FIXED.bot_key, telegram_user_id=8, telegram_chat_id=8,
        )
    finally:
        conn.close()
    h = _harness(settings)
    try:
        user = make_user(8)
        _manual_to_final(h, user, ids)
        h.press(user, "fc:continue")
    finally:
        h.close()
    conn = get_connection(settings.app.db_file)
    try:
        after = get_order_by_id(conn, before.id)
    finally:
        conn.close()
    assert (after.contact_email, after.contact_phone, after.updated_at) == ("old@example.com", "+7 999 111-22-33", before.updated_at)


# --------------------------------------------------------------------- web


def test_web_checkout_still_requires_and_uses_the_customers_own_contacts():
    from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
    from app.dates.rules import today_in_georgia
    from app.deps import get_settings
    from app.main import app
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    conn = get_connection(get_settings().app.db_file)
    upsert_category(conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
    make = upsert_manufacturer(conn, external_id=19001, name="ZFIXEDCONTACTSMAKE", is_popular=False)
    model = upsert_model(conn, external_id=19001, manufacturer_id=make, name="ZFIXEDCONTACTSMODEL")
    mark_models_synced(conn, make)
    conn.commit()
    conn.close()

    client = TestClient(app)
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    client.post("/date", data={"start_date": (today_in_georgia() + timedelta(days=10)).isoformat()})
    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data={"registration_number": "WEB1", "identifier_type": "vin", "identifier": VIN,
                                  "manufacturer_id": str(make), "model_id": str(model)})
    missing = client.post("/policyholder", data=valid_policyholder_data(contact_email=""), follow_redirects=False)
    assert missing.status_code == 422  # still required on the web
    ok = client.post("/policyholder", data=valid_policyholder_data(contact_phone="+7 900 123-45-67"), follow_redirects=False)
    token = ok.headers["location"].split("/")[2]
    conn = get_connection(get_settings().app.db_file)
    try:
        order = get_order_by_token(conn, token)
    finally:
        conn.close()
    assert (order.contact_email, order.contact_phone) == ("ivan@example.com", "+7 900 123-45-67")
