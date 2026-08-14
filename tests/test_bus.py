"""BUS enablement regression tests.

tpl.ge research (2026-08-13) confirmed bus's checkout flow, vehicle fields,
and manufacturer/model catalog are identical to passenger_car/motorcycle/
trailer, and that tpl.ge itself performs no passenger-seat-count (or any
other bus-defining) validation anywhere -- these tests are deliberately
light: they check bus-specific config/state wiring, not general checkout
mechanics already covered by test_routes_smoke.py / test_motorcycle.py /
test_trailer.py / test_documents_ocr_flow.py.

Bus RUB tariffs are INDEPENDENT fixed values confirmed by business, NOT a
formula derived from passenger_car -- test A asserts the literal numbers
rather than any relationship to passenger_car's prices.
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import get_ocr_provider, get_settings
from app.main import app
from app.ocr.models import OcrResult
from app.ocr.provider import FakeOcrProvider

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
# external_id ranges chosen to not collide with test_routes_smoke.py
# (7/12/999/24376/1/3/4), test_documents_ocr_flow.py (5001),
# test_motorcycle.py (6001/6002), or test_trailer.py (7001) -- all share the
# same file-backed test DB (see tests/conftest.py).
upsert_category(_conn, external_id=9, code="bus", name="Автобус", icon="bus")
_bus_manufacturer_id = upsert_manufacturer(_conn, external_id=8001, name="ZBUSFICTIONALMAKE", is_popular=True)
_bus_model_id = upsert_model(_conn, external_id=8001, manufacturer_id=_bus_manufacturer_id, name="ZBUSFICTIONALMODEL")
# Mark models synced -- otherwise match_model_text/on-demand sync (see
# app.ocr.parser / app.catalog.sync) would try a REAL network call to
# tpl.ge for this fictional external_id from inside the test suite.
mark_models_synced(_conn, _bus_manufacturer_id)
_conn.commit()
_conn.close()


@pytest.fixture
def real_production_config(monkeypatch):
    """Same pattern as test_routes_smoke.py / test_motorcycle.py /
    test_trailer.py -- reads the confirmed bus tariffs from the real
    config/config.yaml."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (60, 60, 60)).save(buffer, format="JPEG")
    return buffer.getvalue()


# --------------------------- A: pricing (independent, not a formula) ---------


def test_bus_periods_and_prices_are_independent_fixed_values(real_production_config):
    from app.deps import PROJECT_ROOT
    from app.pricing.provider import available_periods
    from app.settings import load_settings

    settings = load_settings(PROJECT_ROOT)
    bus_periods = {p.code: p for p in available_periods(settings, "GE", "bus")}
    assert list(bus_periods.keys()) == ["15d", "30d", "90d"]
    assert bus_periods["15d"].price_rub == 1999
    assert bus_periods["30d"].price_rub == 3199
    assert bus_periods["90d"].price_rub == 5449
    assert "1y" not in bus_periods

    # Literal independent tariffs, not a passenger_car-derived formula --
    # no single multiplier reproduces all three bus prices from
    # passenger_car's, which is exactly what "independent" must mean here.
    passenger_periods = {p.code: p for p in available_periods(settings, "GE", "passenger_car")}
    ratios = {bus_periods[c].price_rub / passenger_periods[c].price_rub for c in ("15d", "30d", "90d")}
    assert len(ratios) > 1, "bus prices must not be a fixed multiple of passenger_car's"


# --------------------------- B: category UI -----------------------------------


def test_bus_category_becomes_selectable_with_its_three_periods_no_1y(real_production_config):
    real_client = TestClient(app)
    response = real_client.get("/api/periods", params={"category_code": "bus"})
    assert response.status_code == 200
    body = response.json()
    assert [p["code"] for p in body] == ["15d", "30d", "90d"]
    assert [p["price_rub"] for p in body] == [1999, 3199, 5449]

    response = real_client.post(
        "/category-period", data={"category_code": "bus", "period_code": "30d"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"

    response = real_client.get("/category-period")
    assert "1 999" in response.text
    assert "3 199" in response.text
    assert "5 449" in response.text
    assert "1 год" not in response.text
    assert 'data-period-code="1y"' not in response.text


# --------------------------- C: full manual flow ------------------------------


def test_full_manual_flow_bus_reaches_summary_with_correct_category_and_price(real_production_config):
    client_ = TestClient(app)

    response = client_.post(
        "/category-period", data={"category_code": "bus", "period_code": "15d"}, follow_redirects=False
    )
    assert response.status_code == 303

    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "BUS001AA",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000888",
            "manufacturer_id": str(_bus_manufacturer_id),
            "model_id": str(_bus_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"

    response = client_.post(
        "/policyholder",
        data={"full_name": "Ivanov Ivan", "contact_email": "ivan@example.com", "contact_telegram": "@ivan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.vehicle_category_code == "bus"
    assert order.period_code == "15d"
    assert order.price_customer_minor == 1999 * 100

    response = client_.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200
    assert "Автобус" in response.text
    assert "1 999" in response.text


# --------------------------- D: manufacturer/model catalog --------------------


def test_bus_vehicle_step_uses_the_existing_global_catalog(real_production_config):
    """Not re-proving the global-catalog/no-category-restriction finding
    again (already established for motorcycle/trailer) -- just confirming
    bus goes through the same catalog validation path successfully."""
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "bus", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "BUS002BB",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000889",
            "manufacturer_id": str(_bus_manufacturer_id),
            "model_id": str(_bus_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"


# --------------------------- E: documents/OCR route ---------------------------


@pytest.fixture
def fake_provider():
    holder = {"provider": FakeOcrProvider()}
    app.dependency_overrides[get_ocr_provider] = lambda: holder["provider"]
    yield holder
    app.dependency_overrides.pop(get_ocr_provider, None)


def test_bus_documents_flow_uses_the_existing_pipeline_with_no_bus_branching(real_production_config, fake_provider):
    """Never a real OpenAI call -- FakeOcrProvider only."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="BUS003CC",
            vin="JYARJ41E7KA000890",
            chassis_number=None,
            manufacturer="ZBUSFICTIONALMAKE",
            model="ZBUSFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "bus", "period_code": "90d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "documents"})

    response = client_.post(
        "/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    review = client_.get("/vehicle")
    assert review.status_code == 200
    assert 'value="BUS003CC"' in review.text
    assert "ZBUSFICTIONALMAKE" in review.text

    response = client_.post(
        "/policyholder",
        data={"full_name": "Petrov Petr", "contact_email": "petr@example.com", "contact_telegram": "@petr"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.vehicle_category_code == "bus"
    assert order.price_customer_minor == 5449 * 100  # 90d


# --------------------------- F: 1y rejection -----------------------------------


def test_bus_1y_is_rejected_server_side_against_real_config(real_production_config):
    real_client = TestClient(app)
    response = real_client.post(
        "/category-period",
        data={"category_code": "bus", "period_code": "1y"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text


# --------------------------- G: regressions (other categories still sellable) --


def test_other_categories_remain_sellable_and_1y_free_against_real_config(real_production_config):
    from app.deps import PROJECT_ROOT
    from app.pricing.provider import available_periods
    from app.settings import load_settings

    settings = load_settings(PROJECT_ROOT)

    passenger = {p.code: p.price_rub for p in available_periods(settings, "GE", "passenger_car")}
    assert passenger == {"15d": 1349, "30d": 2149, "90d": 3649}

    motorcycle = {p.code: p.price_rub for p in available_periods(settings, "GE", "motorcycle")}
    assert motorcycle == {"15d": 1059, "30d": 1549, "90d": 2899}

    trailer = {p.code: p.price_rub for p in available_periods(settings, "GE", "trailer")}
    assert trailer == {"15d": 849, "30d": 1249, "90d": 1799}
