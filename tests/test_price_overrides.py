"""Admin-editable retail prices: DB overrides on top of config.yaml, one
effective price for the web checkout and the Telegram bot, never touching
existing orders.

Web/admin tests use the shared app DB + tests/fixtures pricing (GE
passenger_car 15d/30d/90d = 1500/2500/4500); every test starts and ends with
no overrides. Bot tests use their own tmp DB + the real config.yaml."""

import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import PROJECT_ROOT, get_settings
from app.main import app
from app.orders.repository import create_order, get_order_by_id, get_order_by_token
from app.pricing import overrides as price_overrides
from app.pricing.provider import available_periods, config_periods, get_period
from app.settings import load_settings
from policyholder_helpers import valid_policyholder_data
from telegram_bot_helpers import BotHarness, last_screen, make_user, seed_catalog

_AUTH = ("admin", "s3cret-test-only")
_START = (today_in_georgia() + timedelta(days=20)).isoformat()

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=17001, name="ZPRICEFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=17001, manufacturer_id=_manufacturer_id, name="ZPRICEFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()


def _clear_overrides():
    conn = get_connection(get_settings().app.db_file)
    try:
        conn.execute("DELETE FROM insurance_price_overrides")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _no_overrides():
    _clear_overrides()
    yield
    _clear_overrides()


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret-test-only")
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _set_override(category="passenger_car", period="30d", price=2600, by="test"):
    conn = get_connection(get_settings().app.db_file)
    try:
        price_overrides.upsert_override(
            conn, country_code="GE", vehicle_category_code=category, period_code=period, price_rub=price, updated_by=by
        )
        conn.commit()
    finally:
        conn.close()


def _overrides():
    conn = get_connection(get_settings().app.db_file)
    try:
        return {(o.vehicle_category_code, o.period_code): o for o in price_overrides.list_overrides(conn, "GE")}
    finally:
        conn.close()


# --------------------------------------------------------------- provider


def test_without_override_config_price_applies():
    settings = get_settings()
    assert available_periods(settings, "GE", "passenger_car") == config_periods(settings, "GE", "passenger_car")
    assert get_period(settings, "GE", "passenger_car", "30d").price_rub == 2500


def test_override_replaces_only_its_own_cell():
    _set_override(price=2600)
    settings = get_settings()
    prices = {p.code: p.price_rub for p in available_periods(settings, "GE", "passenger_car")}
    assert prices["30d"] == 2600
    assert prices["15d"] == 1500 and prices["90d"] == 4500
    assert get_period(settings, "GE", "passenger_car", "30d").price_minor == 260000
    assert {p.code: p.price_rub for p in config_periods(settings, "GE", "passenger_car")}["30d"] == 2500


def test_override_cannot_invent_a_period():
    _set_override(period="7d", price=100)
    codes = [p.code for p in available_periods(get_settings(), "GE", "passenger_car")]
    assert "7d" not in codes


def test_missing_db_means_no_overrides_and_is_never_created(tmp_path):
    missing = tmp_path / "nope.db"
    assert price_overrides.read_override_prices(missing, "GE", "passenger_car") == {}
    assert not missing.exists()


def test_db_without_overrides_table_falls_back(tmp_path):
    import sqlite3

    db_file = tmp_path / "old.db"
    sqlite3.connect(db_file).close()
    assert price_overrides.read_override_prices(db_file, "GE", "passenger_car") == {}


@pytest.mark.parametrize("bad_price", [0, -1, price_overrides.MAX_PRICE_RUB + 1])
def test_repository_rejects_out_of_range_price(bad_price):
    with pytest.raises(ValueError):
        _set_override(price=bad_price)


# ------------------------------------------------------------------ admin


def test_admin_prices_requires_admin_auth(admin):
    assert admin.get("/admin/prices").status_code == 401
    assert admin.post("/admin/prices", data={"price__passenger_car__30d": "1"}).status_code == 401
    assert _overrides() == {}


def test_admin_prices_page_shows_the_catalog_and_config_matrix(admin):
    response = admin.get("/admin/prices", auth=_AUTH)
    assert response.status_code == 200
    assert "Цены — Грузия" in response.text
    assert "Легковой" in response.text
    for label in ("15 дней", "30 дней", "90 дней"):
        assert label in response.text
    assert 'name="price__passenger_car__30d"' in response.text
    assert 'value="2500"' in response.text


def test_admin_changes_price_and_it_is_used_immediately(admin, caplog):
    with caplog.at_level(logging.INFO):
        response = admin.post(
            "/admin/prices",
            data={"price__passenger_car__15d": "1500", "price__passenger_car__30d": "2 650", "price__passenger_car__90d": "4500"},
            auth=_AUTH,
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/prices?saved=1"
    overrides = _overrides()
    assert list(overrides) == [("passenger_car", "30d")]  # unchanged cells are not written
    assert overrides[("passenger_car", "30d")].price_rub == 2650
    assert overrides[("passenger_car", "30d")].updated_by == "admin"
    assert "Retail price changed: GE/passenger_car/30d 2500 -> 2650 RUB by admin 'admin'" in caplog.text
    assert "s3cret-test-only" not in caplog.text

    page = admin.get("/admin/prices", auth=_AUTH)
    assert 'value="2650"' in page.text
    assert "изменено (admin)" in page.text
    # the web checkout sees it at once -- no restart, no cache
    checkout = TestClient(app).get("/category-period")
    assert "2 650" in checkout.text


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "12.5", "", "10000001", "1e3"])
def test_invalid_price_is_rejected_and_nothing_is_saved(admin, bad):
    response = admin.post(
        "/admin/prices",
        data={"price__passenger_car__15d": "1700", "price__passenger_car__30d": bad},
        auth=_AUTH,
    )
    assert response.status_code == 422
    assert "Цены не сохранены" in response.text
    assert _overrides() == {}  # all-or-nothing: the valid 15d change wasn't saved either


def test_unknown_fields_cannot_create_prices(admin):
    admin.post(
        "/admin/prices",
        data={"price__spaceship__30d": "5", "price__passenger_car__7d": "5", "price__passenger_car__30d": "2500"},
        auth=_AUTH,
    )
    assert _overrides() == {}


def test_cross_origin_price_change_is_rejected(admin):
    response = admin.post(
        "/admin/prices",
        data={"price__passenger_car__30d": "1"},
        auth=_AUTH,
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert _overrides() == {}


def test_reset_returns_cell_to_config_default(admin):
    _set_override(price=2600)
    response = admin.post(
        "/admin/prices/reset", data={"category_code": "passenger_car", "period_code": "30d"}, auth=_AUTH, follow_redirects=False
    )
    assert response.status_code == 303
    assert _overrides() == {}
    assert get_period(get_settings(), "GE", "passenger_car", "30d").price_rub == 2500


def test_orders_page_links_to_prices(admin):
    assert 'href="/admin/prices"' in admin.get("/admin/orders", auth=_AUTH).text


# ------------------------------------------------------------ orders/web


def test_existing_order_keeps_its_price_after_a_change():
    conn = get_connection(get_settings().app.db_file)
    try:
        start = today_in_georgia() + timedelta(days=10)
        order = create_order(
            conn,
            session_id="price-sess",
            country_code="GE",
            vehicle_category_code="passenger_car",
            period_code="30d",
            start_date=start,
            end_date=start + timedelta(days=30),
            price_customer_minor=250000,
            data_entry_method="manual",
            registration_number="PRC001",
            identifier_type="vin",
            identifier="JYARJ41E7KA000701",
            manufacturer_id=_manufacturer_id,
            manufacturer_name="M",
            model_id=_model_id,
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
    finally:
        conn.close()
    _set_override(price=3999)
    conn = get_connection(get_settings().app.db_file)
    try:
        assert get_order_by_id(conn, order.id).price_customer_minor == 250000
    finally:
        conn.close()
    summary = TestClient(app).get(f"/o/{order.resume_token}/summary")
    assert "2 500" in summary.text and "3 999" not in summary.text


def test_web_checkout_in_progress_is_priced_at_the_current_price():
    client = TestClient(app)
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    client.post("/date", data={"start_date": _START})
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": "PRC002",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000702",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )
    _set_override(price=2777)  # changed after the customer picked the period
    response = client.post("/policyholder", data=valid_policyholder_data(), follow_redirects=False)
    token = response.headers["location"].split("/")[2]
    conn = get_connection(get_settings().app.db_file)
    try:
        assert get_order_by_token(conn, token).price_customer_minor == 277700
    finally:
        conn.close()


# ------------------------------------------------------------------- bot


@pytest.fixture
def bot_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    settings = load_settings(PROJECT_ROOT)
    seed_catalog(settings.app.db_file)
    return settings


def _bot_override(settings, price, period="30d"):
    conn = get_connection(settings.app.db_file)
    try:
        price_overrides.upsert_override(
            conn, country_code="GE", vehicle_category_code="passenger_car", period_code=period, price_rub=price, updated_by="t"
        )
        conn.commit()
    finally:
        conn.close()


def test_bot_period_buttons_use_the_override(bot_settings):
    _bot_override(bot_settings, 1999)
    harness = BotHarness(bot_settings)
    try:
        user = make_user(501)
        harness.send_text(user, "/start")
        screen = last_screen(harness.press(user, "c:passenger_car"))
        assert "30 дней — 1 999 ₽" in screen.text
        assert "15 дней — 1 349 ₽" in screen.text  # untouched cell: real config price
    finally:
        harness.close()


def test_bot_summary_uses_a_price_changed_after_selection(bot_settings):
    harness = BotHarness(bot_settings)
    try:
        user = make_user(502)
        harness.send_text(user, "/start")
        harness.press(user, "p:passenger_car:30d")
        _bot_override(bot_settings, 2499)
        screen = last_screen(harness.press(user, "d:tomorrow"))
        assert "💰 Стоимость: 2 499 ₽" in screen.text
    finally:
        harness.close()
