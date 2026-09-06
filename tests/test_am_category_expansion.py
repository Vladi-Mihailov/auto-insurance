"""Armenia (AM) category expansion: passenger_car -> all six categories.

Business confirmed 2026-09-06 that Armenia's tariff grid is LITERALLY
Georgia's own tariff grid for the corresponding category -- each AM
category's two reference points (15d/30d) are copied directly from
config.yaml's pricing.GE.<category> 15d/30d prices, no markup, no separate
AM business data. The mechanism itself (exact start/end dates, 10-365 day
range, 15-day default, linear extrapolation, ROUND_HALF_UP) is completely
unchanged from passenger_car (see tests/test_am_linear_pricing.py) -- this
file only covers the five newly-enabled categories' own numbers plus the
category-aware engine_power fix.

engine_power: trailer has no engine, so it must not be shown or required
for AM trailer even though AM otherwise requires it (see
app.web.checkout_routes._requires_engine_power/_CATEGORIES_WITHOUT_ENGINE).
Every other AM category keeps the existing requirement.

All dates are computed relative to today_in_georgia(), never a hardcoded
literal -- same time-bomb lesson as the rest of this test suite.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import get_settings
from app.main import app
from app.pricing.provider import resolve_duration_price

_settings = get_settings()

# Own catalog rows -- external_id=23001 continues the per-file numbering
# convention. All six categories are seeded here (external_ids matching
# app.catalog.sync.CATEGORY_CODE_BY_EXTERNAL_ID) so this file passes in
# isolation, idempotent via ON CONFLICT(external_id).
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
upsert_category(_conn, external_id=8, code="truck", name="Грузовик", icon="truck")
upsert_category(_conn, external_id=9, code="bus", name="Автобус", icon="bus")
upsert_category(_conn, external_id=10, code="motorcycle", name="Мотоцикл", icon="motorcycle")
upsert_category(_conn, external_id=11, code="trailer", name="Прицеп", icon="trailer")
upsert_category(_conn, external_id=12, code="special_vehicle", name="Спецтехника", icon="special_vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=23001, name="ZAMEXPANDFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=23001, manufacturer_id=_manufacturer_id, name="ZAMEXPANDFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()

_START = today_in_georgia() + timedelta(days=60)


def _iso(offset_days: int) -> str:
    return (_START + timedelta(days=offset_days)).isoformat()


@pytest.fixture
def real_config(monkeypatch):
    """AM's per-category duration_ranges/pricing/catalog allow-list live in
    the REAL config/config.yaml, not the test fixture -- same fixture shape
    as tests/test_am_linear_pricing.py."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start(client: TestClient, country: str) -> None:
    client.get("/start", params={"country": country}, follow_redirects=False)


def _vehicle_data(reg, **overrides):
    data = {
        "registration_number": reg,
        "identifier_type": "vin",
        "identifier": "JT123456789012345",
        "manufacturer_id": str(_manufacturer_id),
        "model_id": str(_model_id),
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Control prices: GE's own 15d/30d price is each category's reference point,
# copied literally, no markup. Same formula/rounding as passenger_car.
# ---------------------------------------------------------------------------

_CONTROL_PRICES_RUB = {
    "passenger_car": {10: 1082, 15: 1349, 30: 2149, 45: 2949, 90: 5349, 180: 10149, 365: 20016},
    "motorcycle": {10: 896, 15: 1059, 30: 1549, 45: 2039, 90: 3509, 180: 6449, 365: 12492},
    "trailer": {10: 716, 15: 849, 30: 1249, 45: 1649, 90: 2849, 180: 5249, 365: 10182},
    "bus": {10: 1599, 15: 1999, 30: 3199, 45: 4399, 90: 7999, 180: 15199, 365: 29999},
    "truck": {10: 2166, 15: 2699, 30: 4299, 45: 5899, 90: 10699, 180: 20299, 365: 40032},
    "special_vehicle": {10: 1082, 15: 1349, 30: 2149, 45: 2949, 90: 5349, 180: 10149, 365: 20016},
}


@pytest.mark.parametrize(
    "category_code,duration_days,expected_rub",
    [
        (category, duration_days, expected_rub)
        for category, prices in _CONTROL_PRICES_RUB.items()
        for duration_days, expected_rub in prices.items()
    ],
)
def test_am_category_control_prices_exact(real_config, category_code, duration_days, expected_rub):
    price_minor = resolve_duration_price(get_settings(), "AM", category_code, duration_days)
    assert price_minor == expected_rub * 100


@pytest.mark.parametrize("category_code", ["motorcycle", "bus", "truck", "trailer", "special_vehicle"])
def test_am_category_15d_and_30d_match_ge_literally(real_config, category_code):
    """The whole point of Option B: AM's reference prices ARE Georgia's own
    15d/30d prices for the category, not a markup on them."""
    from app.pricing.provider import available_periods

    ge_periods = {p.code: p.price_rub for p in available_periods(get_settings(), "GE", category_code)}
    assert resolve_duration_price(get_settings(), "AM", category_code, 15) == ge_periods["15d"] * 100
    assert resolve_duration_price(get_settings(), "AM", category_code, 30) == ge_periods["30d"] * 100


@pytest.mark.parametrize("category_code", ["motorcycle", "bus", "truck", "trailer", "special_vehicle"])
def test_am_category_enabled_and_reaches_date_step(real_config, category_code):
    client = TestClient(app)
    _start(client, "AM")
    response = client.post(
        "/category-period", data={"category_code": category_code, "period_code": ""}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"


@pytest.mark.parametrize("category_code", ["motorcycle", "bus", "truck", "trailer", "special_vehicle"])
def test_am_category_boundaries_still_10_to_365(real_config, category_code):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": category_code, "period_code": ""})

    below_min = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(9)})
    assert below_min.status_code == 422

    accepted = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(10)}, follow_redirects=False)
    assert accepted.status_code == 303

    client2 = TestClient(app)
    _start(client2, "AM")
    client2.post("/category-period", data={"category_code": category_code, "period_code": ""})
    accepted_max = client2.post(
        "/date", data={"start_date": _iso(0), "end_date": _iso(365)}, follow_redirects=False
    )
    assert accepted_max.status_code == 303

    above_max = client2.post("/date", data={"start_date": _iso(0), "end_date": _iso(366)})
    assert above_max.status_code == 422


# ---------------------------------------------------------------------------
# engine_power: category-aware -- trailer has no engine.
# ---------------------------------------------------------------------------


def test_am_trailer_vehicle_form_does_not_show_engine_power(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "trailer", "period_code": ""})
    client.post("/date", data={"start_date": _iso(0), "end_date": _iso(15)})
    client.post("/method", data={"choice": "manual"})
    response = client.get("/vehicle")
    assert response.status_code == 200
    assert 'name="engine_power"' not in response.text


def test_am_trailer_order_creation_does_not_require_engine_power(real_config):
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "trailer", "period_code": ""})
    client.post("/date", data={"start_date": _iso(0), "end_date": _iso(15)})
    client.post("/method", data={"choice": "manual"})
    response = client.post("/vehicle", data=_vehicle_data("AMTRAILER1"), follow_redirects=False)
    assert response.status_code == 303  # no engine_power submitted, still accepted

    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_trailer"), follow_redirects=False
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.vehicle_category_code == "trailer"
    assert order.engine_power is None
    assert order.price_customer_minor == 84900  # 15d trailer = 849 RUB

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "Мощность двигателя" not in summary.text
    assert "849" in summary.text


@pytest.mark.parametrize("category_code", ["passenger_car", "motorcycle", "bus", "truck", "special_vehicle"])
def test_am_non_trailer_categories_still_show_and_require_engine_power(real_config, category_code):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": category_code, "period_code": ""})
    client.post("/date", data={"start_date": _iso(0), "end_date": _iso(15)})
    client.post("/method", data={"choice": "manual"})

    form_response = client.get("/vehicle")
    assert 'name="engine_power"' in form_response.text

    response = client.post("/vehicle", data=_vehicle_data(f"AM{category_code[:6].upper()}"), follow_redirects=False)
    assert response.status_code == 422  # engine_power required, not submitted
    assert "Мощность двигателя" in response.text


def test_am_trailer_edit_vehicle_also_omits_engine_power(real_config):
    """Same category-aware rule applies post-order, on the edit-vehicle
    screen (a second render path through the same _vehicle_form_context)."""
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "trailer", "period_code": ""})
    client.post("/date", data={"start_date": _iso(0), "end_date": _iso(15)})
    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data=_vehicle_data("AMTRLEDIT1"))
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_trailer_edit"), follow_redirects=False
    )
    resume_token = response.headers["location"].split("/")[2]

    edit_response = client.get(f"/o/{resume_token}/edit-vehicle")
    assert edit_response.status_code == 200
    assert 'name="engine_power"' not in edit_response.text


# ---------------------------------------------------------------------------
# Full flow for a non-passenger_car category, end to end.
# ---------------------------------------------------------------------------


def test_am_motorcycle_full_flow_order_summary_and_payment(real_config):
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "motorcycle", "period_code": ""})

    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(30)}, follow_redirects=False)
    assert response.status_code == 303

    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data=_vehicle_data("AMMOTO1", engine_power="40"))
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_motorcycle"), follow_redirects=False
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.country_code == "AM"
    assert order.vehicle_category_code == "motorcycle"
    assert order.period_code is None
    assert order.engine_power == 40
    assert order.price_customer_minor == 154900  # 30d motorcycle = 1549 RUB

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "1 549" in summary.text
    assert "30 дней" in summary.text

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    payment = client.get(f"/o/{resume_token}/payment")
    assert payment.status_code == 200
    assert "1 549" in payment.text


# ---------------------------------------------------------------------------
# Regression: GE's own per-category pricing/flow is untouched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category_code,expected",
    [
        ("passenger_car", {"15d": 1349, "30d": 2149, "90d": 3649}),
        ("motorcycle", {"15d": 1059, "30d": 1549, "90d": 2899}),
        ("trailer", {"15d": 849, "30d": 1249, "90d": 1799}),
        ("bus", {"15d": 1999, "30d": 3199, "90d": 5449}),
        ("truck", {"15d": 2699, "30d": 4299, "90d": 7299}),
        ("special_vehicle", {"15d": 1349, "30d": 2149, "90d": 3649}),
    ],
)
def test_ge_category_pricing_unaffected_by_am_expansion(real_config, category_code, expected):
    client = TestClient(app)
    response = client.get("/api/periods", params={"category_code": category_code})
    periods = {p["code"]: p["price_rub"] for p in response.json()}
    assert periods == expected


def test_tr_category_set_unaffected_by_am_expansion(real_config):
    """TR was separately expanded to four categories (passenger_car,
    motorcycle, truck, special_vehicle -- see tests/test_tr_tl_pricing.py)
    around the same time as AM's own six-category expansion, but the two are
    independent decisions -- TR must NOT end up with AM's full six-category
    set (bus/trailer specifically must stay excluded for TR)."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.post("/category-period", data={"category_code": "bus", "period_code": "30d"})
    assert response.status_code == 422
    assert "Выберите категорию транспорта" in response.text
