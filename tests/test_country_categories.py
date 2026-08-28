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

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import upsert_category
from app.db import get_connection
from app.deps import get_settings
from app.main import app

_settings = get_settings()

# Reuses the exact same external_id/code pairs test_truck.py and every other
# module already use for "passenger_car"(7)/"truck"(8) -- upsert_category's
# conflict target is external_id, so this is idempotent onto the same rows
# regardless of which test module happens to run first.
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
upsert_category(_conn, external_id=8, code="truck", name="Грузовик", icon="truck")
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
    """passenger_car is enabled for AM -- the submission must fail on the
    PERIOD check (no AM pricing configured yet), never the category check.
    This is the "accepted server-side" assertion for AM."""
    client = TestClient(app)
    _start(client, "AM")
    response = client.post("/category-period", data={"category_code": "passenger_car", "period_code": "10d"})
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text
    assert "Выберите категорию транспорта" not in response.text


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


def test_tr_passenger_car_clears_the_category_check(real_config):
    client = TestClient(app)
    _start(client, "TR")
    response = client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text
    assert "Выберите категорию транспорта" not in response.text


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


def test_tr_api_periods_is_empty_not_500_and_not_ge_prices(real_config):
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    assert response.status_code == 200
    assert response.json() == []


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
