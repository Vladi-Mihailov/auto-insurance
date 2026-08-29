"""Armenia (AM) linear daily pricing + percent discount.

Two reference points (15d=1499 RUB, 30d=2299 RUB) define a straight
RUB-per-day line -- confirmed by business 2026-08-29 -- applied across the
FULL configured 10-365 day range for AM passenger_car (see
config/config.yaml's duration_ranges.AM.passenger_car.pricing and
app.pricing.provider.resolve_duration_price). Every individual day count is
computed from the formula directly, never bucketed into brackets; days
outside 15-30 are intentionally extrapolated, not rejected -- a deliberate
business rule, not a bug.

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
from app.pricing.provider import resolve_duration_price
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


def _iso(offset_days: int) -> str:
    return (_START + timedelta(days=offset_days)).isoformat()


@pytest.fixture
def real_config(monkeypatch):
    """AM's linear pricing lives in the REAL config/config.yaml, not the
    test fixture -- same fixture shape as tests/test_country_periods.py."""
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


def _settings_with_discount(discount_percent: int):
    """A minimal duck-typed settings-shaped object carrying only what
    resolve_duration_price actually reads -- avoids depending on (or
    mutating) the real global Settings singleton for the discount-specific
    tests below."""
    pricing_config = LinearDurationPricingConfig(
        reference_days_1=15,
        reference_price_rub_1=1499,
        reference_days_2=30,
        reference_price_rub_2=2299,
        discount_percent=discount_percent,
    )
    duration_range_config = DurationRangeConfig(min_days=10, max_days=365, pricing=pricing_config)

    class _FakePricing:
        duration_ranges_by_country_category = {"AM": {"passenger_car": duration_range_config}}

    class _FakeSettings:
        pricing = _FakePricing()

    return _FakeSettings()


# ---------------------------------------------------------------------------
# Control prices: exact values from the business spec.
# formula: base_price(D) = 1499 + (D - 15) * (2299 - 1499) / (30 - 15)
# rounding: ROUND_HALF_UP to whole RUB.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "duration_days,expected_rub",
    [
        (10, 1232),
        (15, 1499),
        (30, 2299),
        (45, 3099),
        (90, 5499),
        (180, 10299),
        (365, 20166),
    ],
)
def test_am_control_prices_exact(real_config, duration_days, expected_rub):
    price_minor = resolve_duration_price(get_settings(), "AM", "passenger_car", duration_days)
    assert price_minor == expected_rub * 100


def test_am_15_days_returns_reference_price_1_exactly(real_config):
    assert resolve_duration_price(get_settings(), "AM", "passenger_car", 15) == 149900


def test_am_30_days_returns_reference_price_2_exactly(real_config):
    assert resolve_duration_price(get_settings(), "AM", "passenger_car", 30) == 229900


# ---------------------------------------------------------------------------
# No bracket rounding: every neighboring day pair must price differently --
# proves the formula is evaluated per exact duration_days, never bucketed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("d_low,d_high", [(14, 15), (15, 16), (29, 30), (30, 31), (89, 90)])
def test_am_neighboring_days_price_differently(real_config, d_low, d_high):
    price_low = resolve_duration_price(get_settings(), "AM", "passenger_car", d_low)
    price_high = resolve_duration_price(get_settings(), "AM", "passenger_car", d_high)
    assert price_low != price_high, (d_low, d_high)


# ---------------------------------------------------------------------------
# Boundaries: unchanged validation (_parse_duration_range_dates), now with a
# real price attached for the accepted cases.
# ---------------------------------------------------------------------------


def test_am_9_days_still_rejected_even_with_real_pricing(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(9)})
    assert response.status_code == 422


def test_am_10_days_accepted_and_priced(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(10)}, follow_redirects=False)
    assert response.status_code == 303
    draft = _read_draft(client)
    assert draft["price_customer_minor"] == 123200


def test_am_365_days_accepted_and_priced(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(365)}, follow_redirects=False)
    assert response.status_code == 303
    draft = _read_draft(client)
    assert draft["price_customer_minor"] == 2016600


def test_am_366_days_still_rejected(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(366)})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Discount: 0% no-op, 10% deterministic ROUND_HALF_UP including a
# fractional-RUB intermediate value.
# ---------------------------------------------------------------------------


def test_am_zero_discount_is_a_no_op(real_config):
    settings = get_settings()
    config = settings.pricing.duration_ranges_by_country_category["AM"]["passenger_car"]
    assert config.pricing.discount_percent == 0
    assert resolve_duration_price(settings, "AM", "passenger_car", 90) == 549900


def test_am_discount_10_percent_with_fractional_intermediate_rounds_half_up():
    """90d base price is 5499 RUB (exact). 10% off: 5499 * 90 / 100 = 4949.1
    RUB exactly -- a fractional RUB amount, per the business spec's own
    worked example -- must round (half up) to 4949 RUB = 494900 minor units,
    never truncate to 4949 by flooring or round to 4950."""
    settings = _settings_with_discount(10)
    assert resolve_duration_price(settings, "AM", "passenger_car", 90) == 494900


def test_am_discount_10_percent_on_an_already_whole_result():
    """30d base price is 2299 RUB. 10% off: 2299 * 90 / 100 = 2069.1 ->
    2069 RUB half-up."""
    settings = _settings_with_discount(10)
    assert resolve_duration_price(settings, "AM", "passenger_car", 30) == 206900


# ---------------------------------------------------------------------------
# Full AM flow: /date populates price_customer_minor in the draft, and it
# flows unchanged through order creation, summary, and payment.
# ---------------------------------------------------------------------------


def test_am_full_flow_price_populates_draft_then_flows_to_order_summary_and_payment(real_config):
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})

    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(90)}, follow_redirects=False)
    assert response.status_code == 303
    draft = _read_draft(client)
    assert draft["price_customer_minor"] == 549900

    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": "AM90DAYS",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
            "engine_power": "150",
        },
    )
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_90d_price"), follow_redirects=False
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.country_code == "AM"
    assert order.period_code is None
    assert order.price_customer_minor == 549900

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "5 499" in summary.text
    assert "90 дней" in summary.text

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    payment = client.get(f"/o/{resume_token}/payment")
    assert payment.status_code == 200
    assert "5 499" in payment.text


# ---------------------------------------------------------------------------
# Edit date: recomputes duration_days -> new price, updates the Order's
# stored price snapshot atomically alongside the dates.
# ---------------------------------------------------------------------------


def test_am_edit_date_recalculates_price_and_updates_order_snapshot(real_config):
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    client.post("/date", data={"start_date": _iso(0), "end_date": _iso(30)})
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": "AMEDITDT",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
            "engine_power": "150",
        },
    )
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_edit_date"), follow_redirects=False
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.price_customer_minor == 229900  # 30d = 2299 RUB

    edit_response = client.post(
        f"/o/{resume_token}/edit-date",
        data={"start_date": _iso(0), "end_date": _iso(45)},
        follow_redirects=False,
    )
    assert edit_response.status_code == 303

    conn = get_connection(_settings.app.db_file)
    try:
        updated_order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert updated_order.end_date.isoformat() == _iso(45)
    assert updated_order.price_customer_minor == 309900  # 45d = 3099 RUB


# ---------------------------------------------------------------------------
# Regression: GE/TR pricing is untouched by AM's new linear model.
# ---------------------------------------------------------------------------


def test_ge_pricing_unaffected_by_am_linear_pricing(real_config):
    client = TestClient(app)
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    periods = {p["code"]: p["price_rub"] for p in response.json()}
    assert periods == {"15d": 1349, "30d": 2149, "90d": 3649}


def test_tr_pricing_unaffected_by_am_linear_pricing(real_config):
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    periods = {p["code"]: p["price_rub"] for p in response.json()}
    assert periods == {"30d": 2299, "45d": 2999, "90d": 3999}

    from app.pricing.provider import available_periods

    all_codes = {p.code: p.price_rub for p in available_periods(get_settings(), "TR", "passenger_car")}
    assert all_codes["180d"] is None
    assert all_codes["365d"] is None
