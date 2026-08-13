"""TRUCK enablement regression tests.

tpl.ge research (2026-08-13) confirmed truck's checkout flow, vehicle
fields, and manufacturer/model catalog are identical to passenger_car/
motorcycle/trailer/bus, and that tpl.ge itself performs no mass/axle/body-
type validation anywhere -- these tests are deliberately light: they check
truck-specific config/state wiring, not general checkout mechanics already
covered by test_routes_smoke.py / test_motorcycle.py / test_trailer.py /
test_bus.py / test_documents_ocr_flow.py.

Truck RUB tariffs are INDEPENDENT fixed values confirmed by business (an
early "passenger_car*2" reference point was NOT the final rule) -- test A
asserts the literal numbers, not any formula relative to passenger_car.
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
# test_motorcycle.py (6001/6002), test_trailer.py (7001), or test_bus.py
# (8001) -- all share the same file-backed test DB (see tests/conftest.py).
upsert_category(_conn, external_id=8, code="truck", name="Грузовик", icon="truck")
_truck_manufacturer_id = upsert_manufacturer(_conn, external_id=9001, name="ZTRUCKFICTIONALMAKE", is_popular=True)
_truck_model_id = upsert_model(_conn, external_id=9001, manufacturer_id=_truck_manufacturer_id, name="ZTRUCKFICTIONALMODEL")
# Mark models synced -- otherwise match_model_text/on-demand sync (see
# app.ocr.parser / app.catalog.sync) would try a REAL network call to
# tpl.ge for this fictional external_id from inside the test suite.
mark_models_synced(_conn, _truck_manufacturer_id)
_conn.commit()
_conn.close()


@pytest.fixture
def real_production_config(monkeypatch):
    """Same pattern as test_routes_smoke.py / test_motorcycle.py /
    test_trailer.py / test_bus.py -- reads the confirmed truck tariffs from
    the real config/config.yaml."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (100, 100, 100)).save(buffer, format="JPEG")
    return buffer.getvalue()


# --------------------------- A: pricing (independent, not a formula) ---------


def test_truck_periods_and_prices_are_independent_literal_values(real_production_config):
    """No passenger_car*2 (or any other) formula -- that was only an early
    reference point, not the final product rule."""
    from app.deps import PROJECT_ROOT
    from app.pricing.provider import available_periods
    from app.settings import load_settings

    settings = load_settings(PROJECT_ROOT)
    truck_periods = {p.code: p for p in available_periods(settings, "GE", "truck")}
    assert list(truck_periods.keys()) == ["15d", "30d", "90d"]
    assert truck_periods["15d"].price_rub == 2699
    assert truck_periods["30d"].price_rub == 4299
    assert truck_periods["90d"].price_rub == 7299
    assert "1y" not in truck_periods

    passenger_periods = {p.code: p for p in available_periods(settings, "GE", "passenger_car")}
    for code in ("15d", "30d", "90d"):
        assert truck_periods[code].price_rub != passenger_periods[code].price_rub * 2


# --------------------------- B: category UI -----------------------------------


def test_truck_category_becomes_selectable_with_its_three_periods_no_1y(real_production_config):
    real_client = TestClient(app)
    response = real_client.get("/api/periods", params={"category_code": "truck"})
    assert response.status_code == 200
    body = response.json()
    assert [p["code"] for p in body] == ["15d", "30d", "90d"]
    assert [p["price_rub"] for p in body] == [2699, 4299, 7299]

    response = real_client.post(
        "/category-period", data={"category_code": "truck", "period_code": "30d"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"

    response = real_client.get("/category-period")
    assert "2 699" in response.text
    assert "4 299" in response.text
    assert "7 299" in response.text
    assert "1 год" not in response.text
    assert 'data-period-code="1y"' not in response.text


# --------------------------- C: full manual flow ------------------------------


def test_full_manual_flow_truck_reaches_summary_with_correct_category_and_price(real_production_config):
    client_ = TestClient(app)

    response = client_.post(
        "/category-period", data={"category_code": "truck", "period_code": "15d"}, follow_redirects=False
    )
    assert response.status_code == 303

    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "TRK001AA",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000601",
            "manufacturer_id": str(_truck_manufacturer_id),
            "model_id": str(_truck_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"

    response = client_.post(
        "/policyholder",
        data={"full_name": "Ivanov Ivan", "contact_type": "telegram", "contact_value": "@ivan"},
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
    assert order.vehicle_category_code == "truck"
    assert order.period_code == "15d"
    assert order.price_customer_minor == 269900

    response = client_.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200
    assert "Грузовик" in response.text
    assert "2 699" in response.text


# --------------------------- D: manufacturer/model catalog --------------------


def test_truck_vehicle_step_uses_the_existing_global_catalog(real_production_config):
    """Not re-proving the global-catalog/no-category-restriction finding
    again (already established for motorcycle/trailer/bus) -- just
    confirming truck goes through the same catalog validation path
    successfully."""
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "truck", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "TRK002BB",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000602",
            "manufacturer_id": str(_truck_manufacturer_id),
            "model_id": str(_truck_model_id),
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


def test_truck_documents_flow_uses_the_existing_pipeline_with_no_truck_branching(
    real_production_config, fake_provider
):
    """Never a real OpenAI call -- FakeOcrProvider only."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="TRK003CC",
            vin="JYARJ41E7KA000603",
            chassis_number=None,
            manufacturer="ZTRUCKFICTIONALMAKE",
            model="ZTRUCKFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "truck", "period_code": "90d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "documents"})

    response = client_.post(
        "/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    review = client_.get("/vehicle")
    assert review.status_code == 200
    assert 'value="TRK003CC"' in review.text
    assert "ZTRUCKFICTIONALMAKE" in review.text

    response = client_.post(
        "/policyholder",
        data={"full_name": "Petrov Petr", "contact_type": "telegram", "contact_value": "@petr"},
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
    assert order.vehicle_category_code == "truck"
    assert order.price_customer_minor == 7299 * 100  # 90d


# --------------------------- F: 1y rejection -----------------------------------


def test_truck_1y_is_rejected_server_side_against_real_config(real_production_config):
    real_client = TestClient(app)
    response = real_client.post(
        "/category-period",
        data={"category_code": "truck", "period_code": "1y"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text


# --------------------------- G: existing category regression -------------------


def test_existing_categories_remain_unchanged_and_1y_free_against_real_config(real_production_config):
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

    bus = {p.code: p.price_rub for p in available_periods(settings, "GE", "bus")}
    assert bus == {"15d": 1999, "30d": 3199, "90d": 5449}
