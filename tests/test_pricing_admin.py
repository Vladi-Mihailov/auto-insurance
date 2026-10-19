"""app.pricing.admin -- the one price-editing service behind both
/admin/prices and the Telegram bot's "💰 Цены" -- plus the GE/TR web admin
and cross-surface checks, against the REAL config/config.yaml (GE fixed RUB
tariffs, TR TL-derived tariffs, the TR bot's own "🚛 Truck / Camper") and a
tmp DB. Never touches data/insurance.db."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_connection
from app.deps import PROJECT_ROOT, get_settings
from app.main import app
from app.orders.repository import create_order, get_order_by_id
from app.pricing import admin as price_admin
from app.pricing import overrides as price_overrides
from app.pricing.provider import available_periods
from app.settings import load_settings
from app.telegram_bot import texts
from app.telegram_bot.keyboards import PricesCb, StaffCb
from app.telegram_bot.profile import BotProfile
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, seed_catalog

_AUTH = ("admin", "s3cret-test-only")
_SAME_ORIGIN = {"Origin": "http://testserver"}
OWNER = 999


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "pricing.db"))
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret-test-only")
    get_settings.cache_clear()
    loaded = load_settings(PROJECT_ROOT)
    seed_catalog(loaded.app.db_file)
    yield loaded
    get_settings.cache_clear()


@pytest.fixture
def conn(settings):
    connection = get_connection(settings.app.db_file)
    yield connection
    connection.close()


@pytest.fixture
def web(settings):
    return TestClient(app)


def _effective(settings, country, category):
    return {p.code: p.price_rub for p in available_periods(settings, country, category)}


def _history(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM insurance_price_override_history ORDER BY id")]


def _set(conn, settings, country, category, period, expected, new, actor="t"):
    return price_admin.set_price(
        conn, settings, country_code=country, category_code=category, period_code=period,
        expected_rub=expected, new_rub=new, actor=actor, source=price_admin.SOURCE_WEB,
    )


def _reset(conn, settings, country, category, period, expected, actor="t"):
    return price_admin.reset_price(
        conn, settings, country_code=country, category_code=category, period_code=period,
        expected_override_rub=expected, actor=actor, source=price_admin.SOURCE_WEB,
    )


# ------------------------------------------------------------------ matrix


def test_ge_matrix_is_the_real_config(conn, settings):
    matrix = price_admin.price_matrix(conn, settings, "GE")
    assert {r.category_code for r in matrix.rows} == {"passenger_car", "motorcycle", "bus", "truck", "trailer", "special_vehicle"}
    assert all(r.channels == ("web", "bot") for r in matrix.rows)  # the GE bot sells the website list
    assert matrix.periods == [("15d", "15 дней"), ("30d", "30 дней"), ("90d", "90 дней")]
    row = next(r for r in matrix.rows if r.category_code == "passenger_car")
    assert {c: cell.base_rub for c, cell in row.cells.items()} == {"15d": 1349, "30d": 2149, "90d": 3649}
    assert all(not cell.has_override for r in matrix.rows for cell in r.cells.values())


def test_tr_matrix_includes_website_only_and_bot_only_categories(conn, settings):
    matrix = price_admin.price_matrix(conn, settings, "TR")
    rows = {r.category_code: r for r in matrix.rows}
    assert list(rows) == ["passenger_car", "motorcycle", "truck", "special_vehicle", "bus"]
    assert rows["passenger_car"].channels == ("web", "bot")
    assert rows["motorcycle"].channels == ("web", "bot")
    assert rows["truck"].channels == ("web",)
    assert rows["special_vehicle"].channels == ("web",)
    assert rows["bus"].channels == ("bot",)
    assert rows["bus"].label == "🚛 Truck / Camper"
    assert [c for c, _ in matrix.periods] == ["30d", "45d", "90d", "180d", "365d"]
    assert set(rows["truck"].cells) == {"30d", "90d"}
    assert {c: cell.effective_rub for c, cell in rows["passenger_car"].cells.items()} == {
        "30d": 1999, "45d": 2399, "90d": 2899, "180d": 9799, "365d": 14099
    }
    assert {c: cell.effective_rub for c, cell in rows["truck"].cells.items()} == {"30d": 2599, "90d": 3499}
    assert {c: cell.effective_rub for c, cell in rows["special_vehicle"].cells.items()} == {"30d": 6199, "90d": 8199}


def test_tr_truck_camper_cells_are_unpriced(conn, settings):
    bus = next(r for r in price_admin.price_matrix(conn, settings, "TR").rows if r.category_code == "bus")
    assert set(bus.cells) == {"30d", "90d"}
    assert all(not cell.is_priced and cell.base_rub is None for cell in bus.cells.values())


@pytest.mark.parametrize("country,base_90d", [("GE", 3649), ("TR", 2899)])
def test_matrix_effective_price_is_exactly_what_checkout_uses(conn, settings, country, base_90d):
    _set(conn, settings, country, "passenger_car", "90d", base_90d, 4444)
    for row in price_admin.price_matrix(conn, settings, country).rows:
        assert {c: cell.effective_rub for c, cell in row.cells.items()} == _effective(settings, country, row.category_code)


# --------------------------------------------------------------- isolation


def test_country_isolation(conn, settings):
    _set(conn, settings, "TR", "passenger_car", "30d", 1999, 2099)
    assert _effective(settings, "TR", "passenger_car")["30d"] == 2099
    assert _effective(settings, "GE", "passenger_car")["30d"] == 2149
    assert price_overrides.list_overrides(conn, "GE") == []


def test_category_isolation(conn, settings):
    _set(conn, settings, "TR", "motorcycle", "30d", 1999, 2199)
    assert _effective(settings, "TR", "motorcycle")["30d"] == 2199
    assert _effective(settings, "TR", "passenger_car")["30d"] == 1999


def test_period_isolation(conn, settings):
    _set(conn, settings, "GE", "passenger_car", "30d", 2149, 2299)
    assert _effective(settings, "GE", "passenger_car") == {"15d": 1349, "30d": 2299, "90d": 3649}


# ------------------------------------------------------------------- writes


def test_set_override_and_history_row(conn, settings):
    change = _set(conn, settings, "GE", "truck", "15d", 2699, 2899, actor="admin")
    assert (change.action, change.old_rub, change.new_rub) == ("set", 2699, 2899)
    (override,) = price_overrides.list_overrides(conn, "GE")
    assert (override.vehicle_category_code, override.period_code, override.price_rub, override.updated_by) == (
        "truck", "15d", 2899, "admin"
    )
    (row,) = _history(conn)
    assert {k: row[k] for k in ("country_code", "vehicle_category_code", "period_code", "action", "old_price_rub",
                                "new_price_rub", "actor", "source")} == {
        "country_code": "GE", "vehicle_category_code": "truck", "period_code": "15d", "action": "set",
        "old_price_rub": 2699, "new_price_rub": 2899, "actor": "admin", "source": "web",
    }
    assert row["created_at"]


def test_reset_override_and_history_row(conn, settings):
    _set(conn, settings, "GE", "truck", "15d", 2699, 2899)
    change = _reset(conn, settings, "GE", "truck", "15d", 2899, actor="admin")
    assert (change.action, change.old_rub, change.new_rub) == ("reset", 2899, 2699)
    assert price_overrides.list_overrides(conn, "GE") == []
    assert _effective(settings, "GE", "truck")["15d"] == 2699
    reset_row = _history(conn)[-1]
    assert (reset_row["action"], reset_row["old_price_rub"], reset_row["new_price_rub"], reset_row["actor"]) == (
        "reset", 2899, 2699, "admin"
    )


def test_saving_the_base_price_removes_the_override(conn, settings):
    _set(conn, settings, "GE", "bus", "30d", 3199, 3399)
    change = _set(conn, settings, "GE", "bus", "30d", 3399, 3199)
    assert change.new_rub == 3199
    assert price_overrides.list_overrides(conn, "GE") == []
    assert [(r["action"], r["old_price_rub"], r["new_price_rub"]) for r in _history(conn)] == [
        ("set", 3199, 3399), ("set", 3399, 3199)
    ]


def test_saving_the_current_price_is_a_no_op(conn, settings):
    assert _set(conn, settings, "GE", "bus", "30d", 3199, 3199) is None
    assert price_overrides.list_overrides(conn, "GE") == [] and _history(conn) == []


def test_unpriced_cell_can_receive_an_override_and_reset_makes_it_unpriced_again(conn, settings):
    _set(conn, settings, "TR", "bus", "30d", None, 4999)
    periods = {p.code: p for p in available_periods(settings, "TR", "bus")}
    assert periods["30d"].price_rub == 4999 and periods["30d"].is_priced
    assert not periods["90d"].is_priced
    _reset(conn, settings, "TR", "bus", "30d", 4999)
    assert not {p.code: p for p in available_periods(settings, "TR", "bus")}["30d"].is_priced
    assert [(r["old_price_rub"], r["new_price_rub"]) for r in _history(conn)] == [(None, 4999), (4999, None)]


def test_stale_set_is_rejected_and_nothing_is_written(conn, settings):
    _set(conn, settings, "GE", "passenger_car", "30d", 2149, 2299, actor="manager B")
    with pytest.raises(price_admin.StalePrice) as exc:
        _set(conn, settings, "GE", "passenger_car", "30d", 2149, 2399, actor="manager A")  # A still saw 2149
    assert exc.value.cell.effective_rub == 2299
    assert _effective(settings, "GE", "passenger_car")["30d"] == 2299
    assert len(_history(conn)) == 1


def test_stale_reset_is_rejected(conn, settings):
    _set(conn, settings, "GE", "passenger_car", "30d", 2149, 2299)
    _set(conn, settings, "GE", "passenger_car", "30d", 2299, 2499)
    with pytest.raises(price_admin.StalePrice):
        _reset(conn, settings, "GE", "passenger_car", "30d", 2299)
    assert _effective(settings, "GE", "passenger_car")["30d"] == 2499


@pytest.mark.parametrize(
    "category,period", [("spaceship", "30d"), ("passenger_car", "7d"), ("passenger_car", "45d"), ("trailer", "30d")]
)
def test_unknown_cells_are_rejected(conn, settings, category, period):
    # GE has no 45d; TR has no trailer -- each country's own config decides.
    country = "TR" if category == "trailer" else "GE"
    with pytest.raises(price_admin.UnknownCell):
        _set(conn, settings, country, category, period, None, 1000)
    with pytest.raises(price_admin.UnknownCell):
        _reset(conn, settings, country, category, period, None)
    assert _history(conn) == []


@pytest.mark.parametrize("bad", [0, -1, price_overrides.MAX_PRICE_RUB + 1])
def test_out_of_range_price_is_rejected(conn, settings, bad):
    with pytest.raises(ValueError):
        _set(conn, settings, "GE", "passenger_car", "30d", 2149, bad)
    assert price_overrides.list_overrides(conn, "GE") == []


@pytest.mark.parametrize("raw", ["", "0", "-5", "12.5", "1e3", "abc", "1000001"])
def test_parse_price_rejects_malformed_input(raw):
    price, error = price_overrides.parse_price(raw)
    assert price is None and error


def test_parse_price_accepts_thousand_separators():
    assert price_overrides.parse_price("2 650") == (2650, None)
    assert price_overrides.parse_price("1 000 000") == (1_000_000, None)


# ---------------------------------------------------------------------- web


def test_web_tr_tab_shows_every_tr_category(web, settings):
    response = web.get("/admin/prices?country=TR", auth=_AUTH)
    assert response.status_code == 200
    assert "Цены — 🇹🇷 Турция" in response.text
    assert 'aria-current="page">🇹🇷 Турция' in response.text
    for code in ("passenger_car", "motorcycle", "truck", "special_vehicle", "bus"):
        assert f'id="cell-{code}-30d"' in response.text
    assert "🚛 Truck / Camper" in response.text
    assert response.text.count("не задана — недоступно к покупке") == 2  # bus 30d + 90d
    assert "1 999 ₽" in response.text and "14 099 ₽" in response.text
    assert 'id="cell-truck-45d"' not in response.text  # TR truck has no 45d period


def test_web_ge_tab_is_the_default(web, settings):
    response = web.get("/admin/prices", auth=_AUTH)
    assert 'aria-current="page">🇬🇪 Грузия' in response.text
    assert 'id="cell-passenger_car-15d"' in response.text and "1 349 ₽" in response.text


def test_unpriced_cell_does_not_block_editing_another_cell(web, settings):
    response = web.post(
        "/admin/prices/set",
        data={"country": "TR", "category_code": "truck", "period_code": "30d", "expected_rub": "2599", "price": "2699"},
        auth=_AUTH, headers=_SAME_ORIGIN, follow_redirects=False,
    )
    assert response.status_code == 303
    assert _effective(settings, "TR", "truck")["30d"] == 2699
    assert not {p.code: p for p in available_periods(settings, "TR", "bus")}["30d"].is_priced


def test_cross_surface_web_to_tr_bot(web, settings):
    response = web.post(
        "/admin/prices/set",
        data={"country": "TR", "category_code": "bus", "period_code": "30d", "expected_rub": "", "price": "4 999"},
        auth=_AUTH, headers=_SAME_ORIGIN, follow_redirects=False,
    )
    assert response.status_code == 303
    profile = BotProfile.from_config("osago24rt", settings.telegram_bot_profiles["osago24rt"])
    harness = BotHarness(settings, profile=profile)
    try:
        customer = make_user(601)
        harness.send_text(customer, "/start")
        screen = last_screen(harness.press(customer, "c:bus"))
        assert "30 дней — 4 999 ₽" in screen.text
        assert "90 дней" not in screen.text  # still unpriced -> not offered
    finally:
        harness.close()


def test_cross_surface_telegram_to_web(web, settings, conn):
    harness = BotHarness(settings, profile=TEST_PROFILE, manager_ids={OWNER})
    try:
        owner = make_user(OWNER)
        harness.send_text(owner, "/start")
        harness.press(owner, StaffCb(a="menu").pack())
        harness.press(owner, PricesCb(a="menu").pack())
        harness.press(owner, PricesCb(a="cat", c="passenger_car").pack())
        harness.press(owner, PricesCb(a="period", c="passenger_car", p="30d").pack())
        confirm = last_screen(harness.send_text(owner, "2 345"))
        harness.press(owner, dict(buttons(confirm))[texts.BTN_PRICES_CONFIRM])
    finally:
        harness.close()

    page = web.get("/admin/prices?country=GE", auth=_AUTH)
    assert 'name="expected_rub" value="2345"' in page.text
    assert f"изменил: telegram manager {OWNER}" in page.text
    (row,) = _history(conn)
    assert (row["source"], row["actor"], row["old_price_rub"], row["new_price_rub"]) == (
        "telegram", f"telegram manager {OWNER}", 2149, 2345
    )


def test_historical_order_keeps_its_price_after_a_web_change(web, settings, conn):
    start = date.today() + timedelta(days=10)
    order = create_order(
        conn, session_id="hist-sess", country_code="TR", vehicle_category_code="passenger_car", period_code="30d",
        start_date=start, end_date=start + timedelta(days=30), price_customer_minor=199900, data_entry_method="manual",
        registration_number="HIST01", identifier_type="vin", identifier="JYARJ41E7KA000703", manufacturer_id=None,
        manufacturer_name="M", model_id=None, model_name="X", full_name="Ivanov Ivan", contact_email="a@example.com",
        contact_telegram=None, contact_phone=None, contact_max=None, contact_other=None, customer_currency="RUB",
        purchase_currency="TRY",
    )
    web.post(
        "/admin/prices/set",
        data={"country": "TR", "category_code": "passenger_car", "period_code": "30d", "expected_rub": "1999", "price": "2999"},
        auth=_AUTH, headers=_SAME_ORIGIN,
    )
    assert _effective(settings, "TR", "passenger_car")["30d"] == 2999
    assert get_order_by_id(conn, order.id).price_customer_minor == 199900
