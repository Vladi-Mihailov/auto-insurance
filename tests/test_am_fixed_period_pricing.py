"""Armenia (AM) fixed-period pricing (replaces the old linear daily pricing).

Until 2026-10-20 AM was an EXACT DATE RANGE product: the customer picked
start_date AND end_date (10-365 days) and the price came from a linear
RUB-per-day line (duration_ranges.AM in config.yaml, via
app.pricing.provider.resolve_duration_price). Business then moved AM to
FIXED PERIODS exactly like Georgia: 15d/30d/90d per category, literal
pricing.AM prices in config.yaml (passenger_car 1399/2299/3999), the end
date computed from the period (app.checkout.rules._FIXED_DURATION_DATE_RULES).
See tests/test_am_category_expansion.py for the other five categories.

The generic duration-range engine (resolve_duration_price) still exists but
no country uses it any more -- its formula/rounding coverage below runs
against a synthetic settings object, never the real config.

All dates are computed relative to today_in_georgia(), never a hardcoded
literal -- same time-bomb lesson as tests/test_country_periods.py.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import SESSION_COOKIE_NAME, get_settings
from app.main import app
from app.pricing.provider import get_duration_range, resolve_duration_price
from app.sessions.repository import get_draft
from app.settings import DurationRangeConfig, LinearDurationPricingConfig

_settings = get_settings()

# Own catalog rows -- external_id=20001 continues the per-file numbering
# convention (category external_id=7/"passenger_car" is the one
# deliberately-shared id every file reuses, idempotent via
# ON CONFLICT(external_id)).
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=20001, name="ZAMPRICINGFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=20001, manufacturer_id=_manufacturer_id, name="ZAMPRICINGFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()

_START = today_in_georgia() + timedelta(days=60)

_AM_CATEGORIES = ("passenger_car", "motorcycle", "bus", "truck", "trailer", "special_vehicle")


def _iso(offset_days: int) -> str:
    return (_START + timedelta(days=offset_days)).isoformat()


@pytest.fixture
def real_config(monkeypatch):
    """AM's pricing.AM lives in the REAL config/config.yaml, not the test
    fixture -- same fixture shape as tests/test_country_periods.py."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start(client: TestClient, country: str) -> None:
    client.get("/start", params={"country": country}, follow_redirects=False)


def _session_id(client: TestClient) -> str:
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id
    return session_id


def _read_draft(client: TestClient) -> dict:
    conn = get_connection(_settings.app.db_file)
    try:
        return get_draft(conn, _session_id(client)) or {}
    finally:
        conn.close()


def _create_am_order(client: TestClient, *, period_code: str, registration_number: str, telegram: str) -> str:
    from policyholder_helpers import valid_policyholder_data

    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": period_code})
    client.post("/date", data={"start_date": _iso(0)})
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": registration_number,
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
            "engine_power": "150",
        },
    )
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram=telegram), follow_redirects=False
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def _synthetic_settings(discount_percent: int = 0):
    """A minimal duck-typed settings-shaped object carrying only what
    resolve_duration_price actually reads -- a synthetic "XX" country, so
    the engine's own coverage never depends on (or mutates) the real
    config, which no longer configures any duration range at all."""
    pricing_config = LinearDurationPricingConfig(
        reference_days_1=15,
        reference_price_rub_1=1349,
        reference_days_2=30,
        reference_price_rub_2=2149,
        discount_percent=discount_percent,
    )
    duration_range_config = DurationRangeConfig(min_days=10, max_days=365, pricing=pricing_config)

    class _FakePricing:
        duration_ranges_by_country_category = {"XX": {"passenger_car": duration_range_config}}

    class _FakeSettings:
        pricing = _FakePricing()

    return _FakeSettings()


# ---------------------------------------------------------------------------
# AM is no longer a duration-range product in the real config.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category_code", _AM_CATEGORIES)
def test_am_has_no_duration_range_and_no_linear_price(real_config, category_code):
    settings = get_settings()
    assert get_duration_range(settings, "AM", category_code) is None
    assert resolve_duration_price(settings, "AM", category_code, 30) is None


def test_am_passenger_car_period_prices(real_config):
    client = TestClient(app)
    _start(client, "AM")
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    periods = {p["code"]: p["price_rub"] for p in response.json()}
    assert periods == {"15d": 1399, "30d": 2299, "90d": 3999}


# ---------------------------------------------------------------------------
# Generic duration-range engine (synthetic settings, no country uses it).
# formula: base_price(D) = 1349 + (D - 15) * (2149 - 1349) / (30 - 15)
# rounding: ROUND_HALF_UP to whole RUB.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "duration_days,expected_rub",
    [(10, 1082), (15, 1349), (30, 2149), (45, 2949), (90, 5349), (180, 10149), (365, 20016)],
)
def test_duration_engine_control_prices_exact(duration_days, expected_rub):
    assert resolve_duration_price(_synthetic_settings(), "XX", "passenger_car", duration_days) == expected_rub * 100


@pytest.mark.parametrize("d_low,d_high", [(14, 15), (15, 16), (29, 30), (30, 31), (89, 90)])
def test_duration_engine_neighboring_days_price_differently(d_low, d_high):
    settings = _synthetic_settings()
    assert resolve_duration_price(settings, "XX", "passenger_car", d_low) != resolve_duration_price(
        settings, "XX", "passenger_car", d_high
    )


def test_duration_engine_discount_10_percent_with_fractional_intermediate_rounds_half_up():
    """90d base price is 5349 RUB (exact). 10% off: 5349 * 90 / 100 = 4814.1
    RUB -- a fractional RUB amount -- must round (half up) to 4814 RUB =
    481400 minor units, never round to 4815."""
    assert resolve_duration_price(_synthetic_settings(10), "XX", "passenger_car", 90) == 481400


def test_duration_engine_discount_10_percent_on_a_second_reference_point():
    """30d base price is 2149 RUB. 10% off: 2149 * 90 / 100 = 1934.1 ->
    1934 RUB half-up."""
    assert resolve_duration_price(_synthetic_settings(10), "XX", "passenger_car", 30) == 193400


# ---------------------------------------------------------------------------
# Full AM flow: the period's price populates the draft at /category-period
# and flows unchanged through order creation, summary, and payment.
# ---------------------------------------------------------------------------


def test_am_full_flow_period_price_flows_to_order_summary_and_payment(real_config):
    from app.orders.repository import get_order_by_token

    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    draft = _read_draft(client)
    assert draft["period_code"] == "30d"
    assert draft["price_customer_minor"] == 229900

    resume_token = _create_am_order(client, period_code="30d", registration_number="AM30DAYS", telegram="@am_30d_price")

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.country_code == "AM"
    assert order.period_code == "30d"
    assert order.start_date.isoformat() == _iso(0)
    assert order.end_date.isoformat() == _iso(30)
    assert order.price_customer_minor == 229900

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "2 299" in summary.text
    assert "30 дней" in summary.text

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    payment = client.get(f"/o/{resume_token}/payment")
    assert payment.status_code == 200
    assert "2 299" in payment.text


# ---------------------------------------------------------------------------
# Edit date on a NEW (period-coded) AM order: start-date only, end date
# recomputed from the stored period, price snapshot untouched.
# ---------------------------------------------------------------------------


def test_am_edit_date_recomputes_end_from_period_and_keeps_price(real_config):
    from app.orders.repository import get_order_by_token

    client = TestClient(app)
    resume_token = _create_am_order(client, period_code="30d", registration_number="AMEDITDT", telegram="@am_edit_date")

    get_response = client.get(f"/o/{resume_token}/edit-date")
    assert get_response.status_code == 200
    assert 'name="end_date"' not in get_response.text

    edit_response = client.post(
        f"/o/{resume_token}/edit-date",
        # A stray end_date (e.g. from an old cached form) is ignored outright.
        data={"start_date": _iso(5), "end_date": _iso(50)},
        follow_redirects=False,
    )
    assert edit_response.status_code == 303
    assert edit_response.headers["location"] == f"/o/{resume_token}/summary"

    conn = get_connection(_settings.app.db_file)
    try:
        updated_order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert updated_order.period_code == "30d"
    assert updated_order.start_date.isoformat() == _iso(5)
    assert updated_order.end_date.isoformat() == _iso(35)
    assert updated_order.price_customer_minor == 229900  # unchanged, AM 30d = 2299 RUB


# ---------------------------------------------------------------------------
# Regression: GE/TR pricing is untouched by AM's own prices.
# ---------------------------------------------------------------------------


def test_ge_pricing_unaffected_by_am_pricing(real_config):
    client = TestClient(app)
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    periods = {p["code"]: p["price_rub"] for p in response.json()}
    assert periods == {"15d": 1349, "30d": 2149, "90d": 3649}


def test_tr_pricing_unaffected_by_am_pricing(real_config):
    """TR's own prices are the TL-based formula's output (see
    tests/test_tr_tl_pricing.py) -- unrelated to AM's prices."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    periods = {p["code"]: p["price_rub"] for p in response.json()}
    assert periods == {"30d": 1999, "45d": 2399, "90d": 2899, "180d": 9799, "365d": 14099}


# ---------------------------------------------------------------------------
# Public homepage: Armenia card is an active link, teaser is the cheapest
# priced AM passenger_car period (see app.web.routes.landing) -- never a
# hardcoded number. GE/TR cards must stay exactly as they were.
# ---------------------------------------------------------------------------


def test_am_homepage_card_links_to_start_with_country_am(real_config):
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/start?country=AM"' in response.text


def test_am_homepage_no_longer_shows_the_operator_only_placeholder(real_config):
    client = TestClient(app)
    response = client.get("/")
    assert "country-card__operator-link" not in response.text
    assert "Оформить через оператора" not in response.text


def test_am_homepage_teaser_shows_cheapest_am_period(real_config):
    """AM passenger_car 15d = 1399 RUB is the cheapest of 1399/2299/3999."""
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "от 1 399 ₽" in response.text
    assert "от 1 082 ₽" not in response.text  # the old linear 10-day teaser


def test_ge_homepage_unaffected_by_am_change(real_config):
    client = TestClient(app)
    response = client.get("/")
    assert 'href="/start"' in response.text
    assert "от 1 349 ₽" in response.text


def test_tr_homepage_unaffected_by_am_change(real_config):
    client = TestClient(app)
    response = client.get("/")
    assert 'href="/start?country=TR"' in response.text
    assert "от 1 999 ₽" in response.text
