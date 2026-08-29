"""Step 2 of the GE/AM/TR rollout: country-aware vehicle category
availability on the existing "Категория и срок" screen.

The restriction (AM/TR limited to passenger_car for now) lives in the REAL
config/config.yaml (see catalog.enabled_category_codes_by_country there),
not the test fixture -- so most of this file uses `real_config` (mirrors
test_routes_smoke.py's own `real_production_config` fixture) rather than the
dummy pricing fixture tests/fixtures/test_config.yaml normally points at.
Georgia is never in that config map, so GE-focused tests don't need it.

AM/TR still have zero priced periods anywhere (out of scope for this step --
see tests/test_country_routing.py and app.web.checkout_routes' module
docstring), so a passenger_car submission for AM/TR is expected to clear the
CATEGORY check and then still fail on the PERIOD check -- these are
deliberately asserted as two distinct failure modes below, never conflated.
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

# Reuses the exact same external_id/code pairs test_truck.py and every other
# module already use for "passenger_car"(7)/"truck"(8) -- upsert_category's
# conflict target is external_id, so this is idempotent onto the same rows
# regardless of which test module happens to run first. Manufacturer/model
# use external_id=19001 -- next free block in the per-file numbering
# convention (see tests/test_country_routing.py's own comment).
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
upsert_category(_conn, external_id=8, code="truck", name="Грузовик", icon="truck")
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
# Armenia: only passenger_car enabled
# ---------------------------------------------------------------------------


def test_am_category_period_screen_only_shows_passenger_car(real_config):
    client = TestClient(app)
    _start(client, "AM")
    response = client.get("/category-period")
    assert response.status_code == 200
    assert 'data-category-code="passenger_car"' in response.text
    assert 'data-category-code="truck"' not in response.text


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


def test_am_disabled_category_is_rejected(real_config):
    client = TestClient(app)
    _start(client, "AM")
    response = client.post("/category-period", data={"category_code": "truck", "period_code": "15d"})
    assert response.status_code == 422
    assert "Выберите категорию транспорта" in response.text


# ---------------------------------------------------------------------------
# Turkey: only passenger_car enabled
# ---------------------------------------------------------------------------


def test_tr_category_period_screen_only_shows_passenger_car(real_config):
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/category-period")
    assert response.status_code == 200
    assert 'data-category-code="passenger_car"' in response.text
    assert 'data-category-code="truck"' not in response.text


def test_tr_category_period_screen_shows_only_the_priced_30d_tile(real_config):
    """Public TR launch: customer sees exactly one selectable period (30d,
    2299 RUB) -- 45d/90d/180d/365d stay configured in config.yaml as future
    availability (see test_tr_config_still_has_all_five_periods_configured
    below) but are never rendered as tiles."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/category-period")
    assert response.status_code == 200
    assert 'data-period-code="30d"' in response.text
    assert "2 299" in response.text
    for code in ("45d", "90d", "180d", "365d"):
        assert f'data-period-code="{code}"' not in response.text


def test_tr_passenger_car_clears_the_category_check(real_config):
    """passenger_car is enabled for TR -- 45d has no confirmed price yet
    (see config.yaml's pricing.TR.passenger_car -- only 30d is priced as
    of the first confirmed TR price), so a valid category+period still
    fails on the PRICE check specifically, never the category check."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.post("/category-period", data={"category_code": "passenger_car", "period_code": "45d"})
    assert response.status_code == 422
    assert "Цена для этого периода пока не настроена" in response.text
    assert "Выберите категорию транспорта" not in response.text
    assert "Выберите один из доступных периодов" not in response.text


def test_tr_disabled_category_is_rejected(real_config):
    client = TestClient(app)
    _start(client, "TR")
    response = client.post("/category-period", data={"category_code": "truck", "period_code": "30d"})
    assert response.status_code == 422
    assert "Выберите категорию транспорта" in response.text


# ---------------------------------------------------------------------------
# Safety: no GE fallback/leakage for AM/TR, no crash on missing pricing
# ---------------------------------------------------------------------------


def test_am_never_shows_ge_categories_beyond_its_own_allowlist(real_config):
    """Regression guard for the exact failure mode Step 2 explicitly warns
    against: AM/TR must never fall back to Georgia's full category list."""
    client = TestClient(app)
    _start(client, "AM")
    response = client.get("/category-period")
    assert "Прицеп" not in response.text  # trailer -- part of GE's set, not AM's
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


def test_tr_config_still_has_all_five_periods_configured(real_config):
    """The underlying config/availability layer (app.pricing.provider,
    NOT the customer-facing /api/periods endpoint) still has all five TR
    periods -- 45d/90d/180d/365d remain configured future availability
    (price_rub: null), never deleted, even though the customer-facing UI
    only ever offers 30d now (see test_tr_api_periods_only_returns_priced_periods)."""
    from app.pricing.provider import available_periods

    periods = {p.code: p for p in available_periods(get_settings(), "TR", "passenger_car")}
    assert list(periods) == ["30d", "45d", "90d", "180d", "365d"]
    assert periods["30d"].is_priced is True
    assert periods["30d"].price_rub == 2299
    for code in ("45d", "90d", "180d", "365d"):
        assert periods[code].is_priced is False
        assert periods[code].price_rub is None


def test_tr_api_periods_only_returns_priced_periods_not_ge_prices(real_config):
    """Customer-facing endpoint: only 30d (the one priced period) is
    returned -- 45d/90d/180d/365d stay configured (see the test above) but
    are never exposed as selectable options to the browser."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    assert response.status_code == 200
    periods = response.json()
    assert [p["code"] for p in periods] == ["30d"]
    assert periods[0]["is_priced"] is True
    assert periods[0]["price_rub"] == 2299


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
# Public TR launch: edit-coverage must show/accept the same priced-only set
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
            "engine_power": "150",
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


def test_tr_edit_coverage_shows_only_the_priced_30d_tile(real_config):
    client = TestClient(app)
    resume_token = _create_tr_order(client)

    response = client.get(f"/o/{resume_token}/edit-coverage")
    assert response.status_code == 200
    assert 'data-period-code="30d"' in response.text
    for code in ("45d", "90d", "180d", "365d"):
        assert f'data-period-code="{code}"' not in response.text


def test_tr_edit_coverage_rejects_manual_post_of_an_unpriced_period(real_config):
    client = TestClient(app)
    resume_token = _create_tr_order(client)

    response = client.post(
        f"/o/{resume_token}/edit-coverage",
        data={"category_code": "passenger_car", "period_code": "90d"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Цена для этого периода пока не настроена" in response.text

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.period_code == "30d"  # unchanged
