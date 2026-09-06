"""Step 2 of the GE/AM/TR rollout: country-aware vehicle category
availability on the existing "Категория и срок" screen.

The restriction lives in the REAL config/config.yaml (see
catalog.enabled_category_codes_by_country there), not the test fixture --
so most of this file uses `real_config` (mirrors test_routes_smoke.py's own
`real_production_config` fixture) rather than the dummy pricing fixture
tests/fixtures/test_config.yaml normally points at. Georgia is never in
that config map, so GE-focused tests don't need it.

AM was expanded from passenger_car-only to all six categories (business
confirmed 2026-09-06 that Armenia's tariff grid is literally Georgia's own,
per category -- see tests/test_am_category_expansion.py for the full
pricing/flow coverage of the other five). TR was separately expanded
(2026-09-06) from passenger_car-only to four categories --  passenger_car,
motorcycle, truck, special_vehicle -- each with a real, fully-priced TL-based
tariff (see tests/test_tr_tl_pricing.py for the full pricing/flow coverage).
bus and trailer stay excluded for TR: the source (strahovka-turkiye.com) has
no confirmed tariff for either. Every TR period configured is now priced --
there is no more "period exists but price not configured" state for TR, so
tests below that used to check that no longer apply.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import get_settings
from app.main import app

_settings = get_settings()

# Reuses the exact same external_id/code pairs every other module already
# uses for these six categories (see app.catalog.sync.CATEGORY_CODE_BY_EXTERNAL_ID)
# -- upsert_category's conflict target is external_id, so this is idempotent
# onto the same rows regardless of which test module happens to run first.
# All six are seeded here (not just passenger_car/truck) so this file's own
# AM-now-shows-all-six-categories test passes even when run in isolation,
# without depending on another test module having run first and inserted
# them as a side effect. Manufacturer/model use external_id=19001 -- next
# free block in the per-file numbering convention (see
# tests/test_country_routing.py's own comment).
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
upsert_category(_conn, external_id=8, code="truck", name="Грузовик", icon="truck")
upsert_category(_conn, external_id=9, code="bus", name="Автобус", icon="bus")
upsert_category(_conn, external_id=10, code="motorcycle", name="Мотоцикл", icon="motorcycle")
upsert_category(_conn, external_id=11, code="trailer", name="Прицеп", icon="trailer")
upsert_category(_conn, external_id=12, code="special_vehicle", name="Спецтехника", icon="special_vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=19001, name="ZCATEGORIESFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=19001, manufacturer_id=_manufacturer_id, name="ZCATEGORIESFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()


@pytest.fixture
def real_config(monkeypatch):
    """Points get_settings() at the REAL config/config.yaml (where AM/TR's
    category restriction actually lives) instead of the test fixture --
    see app.deps.get_settings's @lru_cache, which needs an explicit
    cache_clear() on both sides for the env var change to take effect."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start(client: TestClient, country: str) -> None:
    client.get("/start", params={"country": country}, follow_redirects=False)


# ---------------------------------------------------------------------------
# Georgia: unchanged
# ---------------------------------------------------------------------------


def test_ge_category_period_screen_still_shows_all_categories():
    client = TestClient(app)
    _start(client, "GE")
    response = client.get("/category-period")
    assert response.status_code == 200
    assert 'data-category-code="passenger_car"' in response.text
    assert 'data-category-code="truck"' in response.text


def test_ge_category_selection_still_works(real_config):
    client = TestClient(app)
    _start(client, "GE")
    response = client.post(
        "/category-period", data={"category_code": "passenger_car", "period_code": "30d"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"


# ---------------------------------------------------------------------------
# Armenia: all six categories enabled (business confirmed 2026-09-06 --
# see tests/test_am_category_expansion.py for the full per-category
# pricing/flow coverage; this file only covers the category-availability
# screen itself).
# ---------------------------------------------------------------------------


def test_am_category_period_screen_shows_all_six_categories(real_config):
    client = TestClient(app)
    _start(client, "AM")
    response = client.get("/category-period")
    assert response.status_code == 200
    for code in ("passenger_car", "motorcycle", "bus", "truck", "trailer", "special_vehicle"):
        assert f'data-category-code="{code}"' in response.text


def test_am_passenger_car_clears_the_category_check(real_config):
    """passenger_car is enabled for AM -- and, as of Step 3, AM's
    passenger_car is an EXACT DATE RANGE product (see
    app.pricing.provider.get_duration_range), so a valid category is now a
    COMPLETE /category-period submission on its own (no period_code to
    reject or accept) -- straight through to /date, never a category
    error. This supersedes Step 2's own version of this test, written
    before AM had a period/date model at all."""
    client = TestClient(app)
    _start(client, "AM")
    response = client.post(
        "/category-period", data={"category_code": "passenger_car", "period_code": ""}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"


def test_am_truck_category_is_now_accepted(real_config):
    """truck used to be rejected for AM (passenger_car-only MVP scope) --
    now that all six categories are enabled, it clears the category check
    exactly like passenger_car does, straight through to /date."""
    client = TestClient(app)
    _start(client, "AM")
    response = client.post(
        "/category-period", data={"category_code": "truck", "period_code": ""}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"


# ---------------------------------------------------------------------------
# Turkey: four categories enabled (passenger_car, motorcycle, truck,
# special_vehicle), each fully priced via the TL-based formula -- see
# tests/test_tr_tl_pricing.py for the formula/rounding coverage itself.
# ---------------------------------------------------------------------------


def test_tr_category_period_screen_shows_the_four_enabled_categories(real_config):
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/category-period")
    assert response.status_code == 200
    for code in ("passenger_car", "motorcycle", "truck", "special_vehicle"):
        assert f'data-category-code="{code}"' in response.text
    for code in ("bus", "trailer"):
        assert f'data-category-code="{code}"' not in response.text


def test_tr_category_period_screen_shows_all_five_passenger_car_periods_priced(real_config):
    """Every configured passenger_car period is now genuinely priced (no
    more "period exists but unpriced" state for TR) -- all five show up as
    selectable tiles with the new TL-formula prices."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/category-period")
    assert response.status_code == 200
    for code in ("30d", "45d", "90d", "180d", "365d"):
        assert f'data-period-code="{code}"' in response.text
    assert "1 999" in response.text
    assert "2 399" in response.text
    assert "2 899" in response.text
    assert "9 799" in response.text
    assert "14 099" in response.text


def test_tr_passenger_car_180d_now_succeeds(real_config):
    """180d used to fail the PRICE check (unconfirmed) -- now that the
    source's own 180d tariff is used, it's a real, selectable, priced
    period like any other."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.post(
        "/category-period", data={"category_code": "passenger_car", "period_code": "180d"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"


def test_tr_truck_and_special_vehicle_are_now_accepted(real_config):
    """truck/special_vehicle used to be rejected for TR (passenger_car-only
    MVP scope) -- now that both are enabled with a real 30d/90d tariff, they
    clear the category+period check exactly like passenger_car does."""
    for category_code in ("truck", "special_vehicle"):
        client = TestClient(app)
        _start(client, "TR")
        response = client.post(
            "/category-period",
            data={"category_code": category_code, "period_code": "30d"},
            follow_redirects=False,
        )
        assert response.status_code == 303, category_code
        assert response.headers["location"] == "/date", category_code


def test_tr_bus_is_still_rejected(real_config):
    """bus has no confirmed source tariff -- stays excluded even though
    other TR categories were just expanded."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.post("/category-period", data={"category_code": "bus", "period_code": "30d"})
    assert response.status_code == 422
    assert "Выберите категорию транспорта" in response.text


def test_tr_truck_45d_is_rejected_as_an_unavailable_period_not_an_unpriced_one(real_config):
    """truck only offers 30d/90d (the source has no 45d tariff for it at
    all) -- requesting 45d must fail as "period doesn't exist for this
    category", the same error an unknown period code gets, NOT the
    separate "price not configured" error (which would wrongly imply the
    period exists and is just pending a price)."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.post("/category-period", data={"category_code": "truck", "period_code": "45d"})
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text
    assert "Цена для этого периода пока не настроена" not in response.text


# ---------------------------------------------------------------------------
# Safety: no GE fallback/leakage for AM/TR, no crash on missing pricing
# ---------------------------------------------------------------------------


def test_tr_never_shows_ge_categories_beyond_its_own_allowlist(real_config):
    """Regression guard for the exact failure mode Step 2 explicitly warns
    against: TR must never fall back to Georgia's full category list. (AM's
    own version of this guard no longer applies -- AM's allowlist now
    equals the full catalog, see test_am_category_period_screen_shows_all_six_categories.)"""
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/category-period")
    assert "Прицеп" not in response.text  # trailer -- part of GE's set, not TR's
    assert "Автобус" not in response.text  # bus -- same


def test_am_api_periods_is_empty_not_500_and_not_ge_prices(real_config):
    """No priced periods exist for AM yet -- the endpoint must return an
    empty list, never a 500/KeyError, and must never silently return
    Georgia's real 15d/30d/90d prices."""
    client = TestClient(app)
    _start(client, "AM")
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    assert response.status_code == 200
    assert response.json() == []


def test_tr_config_has_all_five_passenger_car_periods_all_priced(real_config):
    """The underlying config/availability layer (app.pricing.provider, NOT
    the customer-facing /api/periods endpoint) has exactly the five
    passenger_car periods the source offers, every single one priced via
    the TL-based formula -- there is no more "period exists but unpriced"
    state for TR (see tests/test_tr_tl_pricing.py for the formula itself)."""
    from app.pricing.provider import available_periods

    periods = {p.code: p for p in available_periods(get_settings(), "TR", "passenger_car")}
    assert list(periods) == ["30d", "45d", "90d", "180d", "365d"]
    expected_rub = {"30d": 1999, "45d": 2399, "90d": 2899, "180d": 9799, "365d": 14099}
    for code, price_rub in expected_rub.items():
        assert periods[code].is_priced is True, code
        assert periods[code].price_rub == price_rub, code


def test_tr_api_periods_returns_all_five_passenger_car_periods(real_config):
    """Customer-facing endpoint: all five periods are now genuinely priced
    and selectable -- unlike before, nothing is held back as unconfirmed."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    assert response.status_code == 200
    periods = {p["code"]: p for p in response.json()}
    assert list(periods) == ["30d", "45d", "90d", "180d", "365d"]
    for code in periods:
        assert periods[code]["is_priced"] is True, code


def test_tr_api_periods_for_truck_and_special_vehicle_shows_only_30_90(real_config):
    """truck/special_vehicle only offer 30d/90d -- the source has no other
    tariff for them, so the API must never invent a 45d/180d/365d entry."""
    client = TestClient(app)
    _start(client, "TR")
    for category_code in ("truck", "special_vehicle"):
        response = client.get("/api/periods", params={"category_code": category_code})
        assert response.status_code == 200
        codes = [p["code"] for p in response.json()]
        assert codes == ["30d", "90d"], category_code


def test_tr_api_periods_for_motorcycle_has_no_45d(real_config):
    """motorcycle has no 45d tariff at all on the source -- must never
    appear, priced or not."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/api/periods", params={"category_code": "motorcycle"})
    assert response.status_code == 200
    codes = [p["code"] for p in response.json()]
    assert codes == ["30d", "90d", "180d", "365d"]
    assert "45d" not in codes


def test_am_category_period_get_screen_renders_without_error_and_shows_no_priced_period(real_config):
    """The screen itself (not just the API) must render safely for a
    country with no pricing at all yet -- no 500/KeyError, and the one
    enabled category shows as "coming soon", never a Georgia price."""
    client = TestClient(app)
    _start(client, "AM")
    response = client.get("/category-period")
    assert response.status_code == 200
    assert "Цена уточняется" not in response.text  # AM has no period rows at all, not even unpriced ones
    assert "₽" not in response.text


# ---------------------------------------------------------------------------
# Public TR launch: edit-coverage must show/accept the same priced set.
# ---------------------------------------------------------------------------


def _create_tr_order(client: TestClient) -> str:
    from policyholder_helpers import valid_policyholder_data

    _start(client, "TR")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    client.post("/date", data={"start_date": (today_in_georgia() + timedelta(days=90)).isoformat()})
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": "TR900AA",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
            "model_year": "2020",
        },
    )
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_edit_coverage", date_of_birth="1990-05-20"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def test_tr_edit_coverage_shows_all_five_priced_tiles(real_config):
    client = TestClient(app)
    resume_token = _create_tr_order(client)

    response = client.get(f"/o/{resume_token}/edit-coverage")
    assert response.status_code == 200
    for code in ("30d", "45d", "90d", "180d", "365d"):
        assert f'data-period-code="{code}"' in response.text


def test_tr_edit_coverage_now_accepts_180d(real_config):
    """180d used to be rejected via edit-coverage (unpriced) -- now it's a
    real, priced period like any other, so switching to it succeeds and
    updates the order's stored price to the new period's."""
    client = TestClient(app)
    resume_token = _create_tr_order(client)

    response = client.post(
        f"/o/{resume_token}/edit-coverage",
        data={"category_code": "passenger_car", "period_code": "180d"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.period_code == "180d"
    assert order.price_customer_minor == 979900  # 180d = 9799 RUB


def test_tr_edit_coverage_rejects_a_period_the_category_never_offered(real_config):
    """Same "period doesn't exist for this category" rejection as
    /category-period itself (see test_tr_truck_45d_is_rejected_as_an_unavailable_period_not_an_unpriced_one) --
    exercised here via edit-coverage instead of the pre-order step."""
    client = TestClient(app)
    resume_token = _create_tr_order(client)

    response = client.post(
        f"/o/{resume_token}/edit-coverage",
        data={"category_code": "passenger_car", "period_code": "14d"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.period_code == "30d"  # unchanged
