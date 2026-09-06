"""Turkey (TR) TL-based pricing (business confirmed 2026-09-06).

Source of truth: strahovka-turkiye.com. Formula (fixed, not customizable):

    final_rub = round_to_nearest_X99(source_price_tl * tl_to_rub_rate + markup_rub)

Production parameters: tl_to_rub_rate = 1.80 (manual config value -- no
external FX API), markup_rub = 600. Category mapping from the source's own
classification: "Легковое авто" -> passenger_car, "Мотоцикл" -> motorcycle,
"Kamyonet" -> truck, "Karavan" -> special_vehicle. bus/trailer are NOT
mapped -- the source has no confirmed tariff for either.

This mechanism is TR-only by construction: app.pricing.provider.available_periods
requires country_code == "TR" before ever reading price_tl/tr_tl_conversion
-- see tests below proving GE/AM are structurally unaffected regardless of
what tr_tl_conversion is set to.

All dates are computed relative to today_in_georgia(), never a hardcoded
literal -- same time-bomb lesson as the rest of this test suite.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import get_settings
from app.main import app
from app.pricing.provider import _round_to_nearest_x99, available_periods, get_period, resolve_duration_price
from app.settings import PeriodConfig, TrTlConversionConfig

_settings = get_settings()

# Own catalog rows -- external_id=24001 continues the per-file numbering
# convention. All four TR categories (plus bus, for the "still excluded"
# regression check) are seeded here so this file passes in isolation.
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
upsert_category(_conn, external_id=8, code="truck", name="Грузовик", icon="truck")
upsert_category(_conn, external_id=9, code="bus", name="Автобус", icon="bus")
upsert_category(_conn, external_id=10, code="motorcycle", name="Мотоцикл", icon="motorcycle")
upsert_category(_conn, external_id=12, code="special_vehicle", name="Спецтехника", icon="special_vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=24001, name="ZTRTLFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=24001, manufacturer_id=_manufacturer_id, name="ZTRTLFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()

_START = today_in_georgia() + timedelta(days=60)


def _iso(offset_days: int) -> str:
    return (_START + timedelta(days=offset_days)).isoformat()


@pytest.fixture
def real_config(monkeypatch):
    """TR's TL-based pricing (including tr_tl_conversion) lives in the REAL
    config/config.yaml, not the test fixture -- same fixture shape as
    tests/test_am_linear_pricing.py."""
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


class _FakePricing:
    def __init__(self, periods_by_country_category, tr_tl_conversion=None):
        self.periods_by_country_category = periods_by_country_category
        self.tr_tl_conversion = tr_tl_conversion
        self.duration_ranges_by_country_category = {}


class _FakeSettings:
    def __init__(self, pricing):
        self.pricing = pricing


def _fake_tr_settings(price_tl: int, rate: str, markup_rub: int) -> _FakeSettings:
    """A minimal duck-typed settings-shaped object carrying only what
    available_periods() actually reads -- avoids depending on (or
    mutating) the real global Settings singleton for the
    formula/rounding-focused tests below."""
    period = PeriodConfig(code="30d", label="30 дней", price_tl=price_tl)
    conversion = TrTlConversionConfig(rate=Decimal(rate), markup_rub=markup_rub)
    pricing = _FakePricing(
        periods_by_country_category={"TR": {"passenger_car": [period]}},
        tr_tl_conversion=conversion,
    )
    return _FakeSettings(pricing)


# ---------------------------------------------------------------------------
# Rounding: _round_to_nearest_x99 in isolation, including tie/boundary cases.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal(1950), 1999),  # the business spec's own worked example (750 TL * 1.80 + 600)
        (Decimal(1900), 1899),  # rounds down -- 1900 is 1 away from 1899, 99 away from 1999
        (Decimal(2000), 1999),  # rounds down -- 2000 is 1 away from 1999, 99 away from 2099
        (Decimal(2850), 2899),  # rounds up -- 2850 is 49 away from 2899, 51 away from 2799
        (Decimal(1949), 1999),  # EXACT TIE (equidistant from 1899 and 1999) -- rounds up
        (Decimal(1999), 1999),  # already an X99 value -- idempotent
        (Decimal(0), -1),  # degenerate low end -- -1 is the X99 below 0 (99 above 0 minus 100)
    ],
)
def test_round_to_nearest_x99(value, expected):
    assert _round_to_nearest_x99(value) == expected


# ---------------------------------------------------------------------------
# Formula: TL * rate + markup, via a fake settings object (isolated from the
# real config.yaml numbers).
# ---------------------------------------------------------------------------


def test_tl_conversion_formula_matches_worked_example():
    """750 TL * 1.80 + 600 = 1950 -> 1999, the business spec's own example."""
    settings = _fake_tr_settings(price_tl=750, rate="1.80", markup_rub=600)
    periods = available_periods(settings, "TR", "passenger_car")
    assert periods[0].price_rub == 1999


def test_markup_rub_600_is_additive_before_rounding():
    """Same TL/rate, markup 0 vs 600 -- isolates markup's own effect from
    the rate's, using numbers engineered so rounding doesn't obscure it
    (both land exactly 600 apart even after independent rounding)."""
    no_markup = _fake_tr_settings(price_tl=1000, rate="2", markup_rub=0)
    with_markup = _fake_tr_settings(price_tl=1000, rate="2", markup_rub=600)
    price_no_markup = available_periods(no_markup, "TR", "passenger_car")[0].price_rub
    price_with_markup = available_periods(with_markup, "TR", "passenger_car")[0].price_rub
    assert price_no_markup == 1999  # 2000 -> 1999
    assert price_with_markup == 2599  # 2600 -> 2599
    assert price_with_markup - price_no_markup == 600


def test_rate_change_recomputes_price_with_no_code_change():
    """Changing tl_to_rub_rate alone (same TL, same markup) changes the
    resulting RUB price -- proves the rate is read fresh each call, not
    baked in anywhere."""
    settings_a = _fake_tr_settings(price_tl=1000, rate="1.80", markup_rub=600)
    settings_b = _fake_tr_settings(price_tl=1000, rate="2.00", markup_rub=600)
    price_a = available_periods(settings_a, "TR", "passenger_car")[0].price_rub
    price_b = available_periods(settings_b, "TR", "passenger_car")[0].price_rub
    assert price_a != price_b
    assert price_a == 2399  # 1000*1.80+600=2400 -> 2399
    assert price_b == 2599  # 1000*2.00+600=2600 -> 2599


def test_tr_tl_conversion_never_applied_to_a_period_without_price_tl():
    """A TR period with only price_rub set (no price_tl at all) must be
    completely unaffected by tr_tl_conversion being present -- the
    conversion is opt-in per period, not automatic for the whole country."""
    period = PeriodConfig(code="30d", label="30 дней", price_rub=12345)
    conversion = TrTlConversionConfig(rate=Decimal("1.80"), markup_rub=600)
    pricing = _FakePricing(periods_by_country_category={"TR": {"passenger_car": [period]}}, tr_tl_conversion=conversion)
    settings = _FakeSettings(pricing)
    assert available_periods(settings, "TR", "passenger_car")[0].price_rub == 12345


def test_tr_tl_conversion_never_applied_to_ge_even_if_present_and_period_has_price_tl():
    """The strongest structural guarantee: even if a GE period somehow had
    price_tl set (should never happen in real config, but the code must not
    rely on that), country_code == "TR" is still required -- GE always
    reads price_rub, never converts."""
    period = PeriodConfig(code="15d", label="15 дней", price_rub=1349, price_tl=999999)
    conversion = TrTlConversionConfig(rate=Decimal("1.80"), markup_rub=600)
    pricing = _FakePricing(periods_by_country_category={"GE": {"passenger_car": [period]}}, tr_tl_conversion=conversion)
    settings = _FakeSettings(pricing)
    assert available_periods(settings, "GE", "passenger_car")[0].price_rub == 1349


# ---------------------------------------------------------------------------
# Control prices: exact values from the business spec, real config.yaml,
# production rate=1.80 / markup=600.
# ---------------------------------------------------------------------------


_CONTROL_PRICES_RUB = {
    "passenger_car": {"30d": 1999, "45d": 2399, "90d": 2899, "180d": 9799, "365d": 14099},
    "motorcycle": {"30d": 1999, "90d": 2399, "180d": 3299, "365d": 5099},
    "truck": {"30d": 2599, "90d": 3499},
    "special_vehicle": {"30d": 6199, "90d": 8199},
}


@pytest.mark.parametrize(
    "category_code,period_code,expected_rub",
    [
        (category, period, rub)
        for category, periods in _CONTROL_PRICES_RUB.items()
        for period, rub in periods.items()
    ],
)
def test_tr_control_prices_exact(real_config, category_code, period_code, expected_rub):
    period = get_period(get_settings(), "TR", category_code, period_code)
    assert period is not None, (category_code, period_code)
    assert period.price_rub == expected_rub


# ---------------------------------------------------------------------------
# Period sets differ by category -- only what the source actually offers,
# no null/placeholder periods invented for UI uniformity.
# ---------------------------------------------------------------------------


def test_tr_period_sets_differ_by_category(real_config):
    settings = get_settings()
    assert [p.code for p in available_periods(settings, "TR", "passenger_car")] == [
        "30d", "45d", "90d", "180d", "365d",
    ]
    assert [p.code for p in available_periods(settings, "TR", "motorcycle")] == ["30d", "90d", "180d", "365d"]
    assert [p.code for p in available_periods(settings, "TR", "truck")] == ["30d", "90d"]
    assert [p.code for p in available_periods(settings, "TR", "special_vehicle")] == ["30d", "90d"]


def test_tr_motorcycle_45d_does_not_exist_at_all(real_config):
    """The source has no 45d tariff for motorcycle -- not unpriced, simply
    absent -- so get_period must return None, not a not-yet-priced option."""
    period = get_period(get_settings(), "TR", "motorcycle", "45d")
    assert period is None


@pytest.mark.parametrize("category_code,period_code", [("truck", "45d"), ("special_vehicle", "180d")])
def test_tr_category_period_post_rejects_a_period_the_category_never_offered(real_config, category_code, period_code):
    client = TestClient(app)
    _start(client, "TR")
    response = client.post("/category-period", data={"category_code": category_code, "period_code": period_code})
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text
    assert "Цена для этого периода пока не настроена" not in response.text


def test_tr_category_switch_does_not_carry_over_an_invalid_period_server_side(real_config):
    """Same session, submit passenger_car+180d (valid) first, then submit
    truck+180d (invalid -- truck only has 30d/90d) -- the second submission
    must be evaluated on its own (category_code, period_code) pair, not
    silently accept the previous category's period."""
    client = TestClient(app)
    _start(client, "TR")

    first = client.post(
        "/category-period", data={"category_code": "passenger_car", "period_code": "180d"}, follow_redirects=False
    )
    assert first.status_code == 303

    second = client.post("/category-period", data={"category_code": "truck", "period_code": "180d"})
    assert second.status_code == 422
    assert "Выберите один из доступных периодов" in second.text


# ---------------------------------------------------------------------------
# Full TR flow: motorcycle and truck (at least one non-passenger_car
# category, per the task's own requirement).
# ---------------------------------------------------------------------------


def test_tr_motorcycle_full_flow_order_summary_and_payment(real_config):
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _start(client, "TR")
    response = client.post(
        "/category-period", data={"category_code": "motorcycle", "period_code": "90d"}, follow_redirects=False
    )
    assert response.status_code == 303

    client.post("/date", data={"start_date": _iso(0)}, follow_redirects=False)
    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data=_vehicle_data("TRMOTO1", model_year="2020"))
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_motorcycle", date_of_birth="1990-05-20"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.country_code == "TR"
    assert order.vehicle_category_code == "motorcycle"
    assert order.period_code == "90d"
    assert order.price_customer_minor == 239900  # 90d motorcycle = 2399 RUB
    assert order.engine_power is None  # no longer required/collected for TR
    assert order.model_year == 2020

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "2 399" in summary.text
    assert "Мощность двигателя" not in summary.text

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    payment = client.get(f"/o/{resume_token}/payment")
    assert payment.status_code == 200
    assert "2 399" in payment.text


def test_tr_truck_full_flow_order_summary_and_payment(real_config):
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _start(client, "TR")
    response = client.post(
        "/category-period", data={"category_code": "truck", "period_code": "30d"}, follow_redirects=False
    )
    assert response.status_code == 303

    client.post("/date", data={"start_date": _iso(0)}, follow_redirects=False)
    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data=_vehicle_data("TRTRUCK1", model_year="2018"))
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_truck", date_of_birth="1985-03-10"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.country_code == "TR"
    assert order.vehicle_category_code == "truck"
    assert order.period_code == "30d"
    assert order.price_customer_minor == 259900  # 30d truck = 2599 RUB
    assert order.engine_power is None

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "2 599" in summary.text

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    payment = client.get(f"/o/{resume_token}/payment")
    assert payment.status_code == 200
    assert "2 599" in payment.text


# ---------------------------------------------------------------------------
# Regression: GE pricing and AM pricing/date-range are untouched by TR's
# new TL-based mechanism.
# ---------------------------------------------------------------------------


def test_ge_pricing_unaffected_by_tr_tl_pricing(real_config):
    client = TestClient(app)
    for category_code, expected in (
        ("passenger_car", {"15d": 1349, "30d": 2149, "90d": 3649}),
        ("motorcycle", {"15d": 1059, "30d": 1549, "90d": 2899}),
        ("truck", {"15d": 2699, "30d": 4299, "90d": 7299}),
    ):
        response = client.get("/api/periods", params={"category_code": category_code})
        periods = {p["code"]: p["price_rub"] for p in response.json()}
        assert periods == expected, category_code


def test_am_linear_pricing_unaffected_by_tr_tl_pricing(real_config):
    """AM's own mechanism (duration_ranges/LinearDurationPricingConfig) is a
    completely separate config tree from pricing.TR/tr_tl_conversion --
    spot-check a couple of AM's own control prices."""
    settings = get_settings()
    assert resolve_duration_price(settings, "AM", "passenger_car", 15) == 134900
    assert resolve_duration_price(settings, "AM", "passenger_car", 30) == 214900
    assert resolve_duration_price(settings, "AM", "motorcycle", 15) == 105900


def test_am_exact_date_range_still_works_unaffected(real_config):
    """AM stays an EXACT DATE RANGE product -- unaffected by TR becoming a
    (still FIXED-period) TL-priced product."""
    client = TestClient(app)
    _start(client, "AM")
    response = client.get("/category-period")
    assert response.status_code == 200
    assert 'id="period-grid"' not in response.text
