"""Armenia (AM) category expansion: passenger_car -> all six categories.

AM was opened for all six categories on 2026-09-06 as an EXACT DATE RANGE
product; on 2026-10-20 business moved it to FIXED PERIODS (15d/30d/90d)
exactly like Georgia, each category with its own literal pricing.AM prices
in config.yaml (NOT linked to pricing.GE -- see the comment there). This
file covers every category's own AM numbers and flow plus the
category-aware engine_power rule.

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
from app.deps import SESSION_COOKIE_NAME, get_settings
from app.main import app
from app.sessions.repository import get_draft

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
    """AM's per-category pricing/catalog allow-list live in the REAL
    config/config.yaml, not the test fixture."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start(client: TestClient, country: str) -> None:
    client.get("/start", params={"country": country}, follow_redirects=False)


def _read_draft(client: TestClient) -> dict:
    conn = get_connection(_settings.app.db_file)
    try:
        return get_draft(conn, client.cookies.get(SESSION_COOKIE_NAME)) or {}
    finally:
        conn.close()


def _walk_to_vehicle(client: TestClient, category_code: str, period_code: str = "15d") -> None:
    """Fixed-period AM flow: category + period, then start_date only (the
    end_date is computed from the period, never submitted)."""
    _start(client, "AM")
    client.post("/category-period", data={"category_code": category_code, "period_code": period_code})
    client.post("/date", data={"start_date": _iso(0)})
    client.post("/method", data={"choice": "manual"})


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
# Prices: AM's own literal pricing.AM values, per category and period.
# ---------------------------------------------------------------------------

_AM_PRICES_RUB = {
    "passenger_car": {"15d": 1399, "30d": 2299, "90d": 3999},
    "motorcycle": {"15d": 1099, "30d": 1599, "90d": 2999},
    "bus": {"15d": 2199, "30d": 3399, "90d": 6499},
    "truck": {"15d": 2899, "30d": 4599, "90d": 7699},
    "trailer": {"15d": 899, "30d": 1299, "90d": 1999},
    "special_vehicle": {"15d": 1349, "30d": 2149, "90d": 3649},
}


@pytest.mark.parametrize("category_code", list(_AM_PRICES_RUB))
def test_am_api_periods_returns_am_prices_per_category(real_config, category_code):
    client = TestClient(app)
    _start(client, "AM")
    response = client.get("/api/periods", params={"category_code": category_code})
    assert response.status_code == 200
    periods = {p["code"]: p["price_rub"] for p in response.json()}
    assert periods == _AM_PRICES_RUB[category_code]


@pytest.mark.parametrize(
    "category_code,period_code,expected_rub",
    [
        (category, period_code, price_rub)
        for category, prices in _AM_PRICES_RUB.items()
        for period_code, price_rub in prices.items()
    ],
)
def test_am_category_period_selection_stores_am_price(real_config, category_code, period_code, expected_rub):
    client = TestClient(app)
    _start(client, "AM")
    response = client.post(
        "/category-period", data={"category_code": category_code, "period_code": period_code}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"
    draft = _read_draft(client)
    assert draft["period_code"] == period_code
    assert draft["price_customer_minor"] == expected_rub * 100


@pytest.mark.parametrize("category_code", ["motorcycle", "bus", "truck", "trailer", "special_vehicle"])
def test_am_category_end_date_computed_from_period(real_config, category_code):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": category_code, "period_code": "90d"})
    response = client.post("/date", data={"start_date": _iso(0)}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/method"
    draft = _read_draft(client)
    assert draft["start_date"] == _iso(0)
    assert draft["end_date"] == _iso(90)


# ---------------------------------------------------------------------------
# engine_power: category-aware -- trailer has no engine.
# ---------------------------------------------------------------------------


def test_am_trailer_vehicle_form_does_not_show_engine_power(real_config):
    client = TestClient(app)
    _walk_to_vehicle(client, "trailer")
    response = client.get("/vehicle")
    assert response.status_code == 200
    assert 'name="engine_power"' not in response.text


def test_am_trailer_order_creation_does_not_require_engine_power(real_config):
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _walk_to_vehicle(client, "trailer")
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
    assert order.period_code == "15d"
    assert order.price_customer_minor == 89900  # AM 15d trailer = 899 RUB

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "Мощность двигателя" not in summary.text
    assert "899" in summary.text


@pytest.mark.parametrize("category_code", ["passenger_car", "motorcycle", "bus", "truck", "special_vehicle"])
def test_am_non_trailer_categories_still_show_and_require_engine_power(real_config, category_code):
    client = TestClient(app)
    _walk_to_vehicle(client, category_code)

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
    _walk_to_vehicle(client, "trailer")
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
    client.post("/category-period", data={"category_code": "motorcycle", "period_code": "30d"})

    response = client.post("/date", data={"start_date": _iso(0)}, follow_redirects=False)
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
    assert order.period_code == "30d"
    assert order.end_date.isoformat() == _iso(30)
    assert order.engine_power == 40
    assert order.price_customer_minor == 159900  # AM 30d motorcycle = 1599 RUB

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "1 599" in summary.text
    assert "30 дней" in summary.text

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    payment = client.get(f"/o/{resume_token}/payment")
    assert payment.status_code == 200
    assert "1 599" in payment.text


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
