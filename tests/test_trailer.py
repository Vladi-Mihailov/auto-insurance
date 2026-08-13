"""TRAILER enablement regression tests.

tpl.ge research (2026-08-13) confirmed trailer's checkout flow, vehicle
fields, and manufacturer/model catalog are identical to passenger_car/
motorcycle -- these tests are deliberately light: they check trailer-
specific config/state wiring, not general checkout mechanics already
covered by test_routes_smoke.py / test_motorcycle.py / test_documents_ocr_flow.py.
Chassis (not VIN) gets its own explicit coverage below since it matters more
for trailer in practice than for the other categories.
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
# (7/12/999/24376/1/3/4), test_documents_ocr_flow.py (5001), or
# test_motorcycle.py (6001/6002) -- all share the same file-backed test DB
# (see tests/conftest.py).
upsert_category(_conn, external_id=11, code="trailer", name="Прицеп", icon="trailer")
_trailer_manufacturer_id = upsert_manufacturer(_conn, external_id=7001, name="ZTRAILERFICTIONALMAKE", is_popular=True)
_trailer_model_id = upsert_model(_conn, external_id=7001, manufacturer_id=_trailer_manufacturer_id, name="ZTRAILERFICTIONALMODEL")
# Mark models synced -- otherwise match_model_text/on-demand sync (see
# app.ocr.parser / app.catalog.sync) would try a REAL network call to
# tpl.ge for this fictional external_id from inside the test suite.
mark_models_synced(_conn, _trailer_manufacturer_id)
_conn.commit()
_conn.close()


@pytest.fixture
def real_production_config(monkeypatch):
    """Same pattern as test_routes_smoke.py / test_motorcycle.py -- reads
    the confirmed trailer tariffs from the real config/config.yaml."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (80, 80, 80)).save(buffer, format="JPEG")
    return buffer.getvalue()


# --------------------------- A: pricing --------------------------------------


def test_trailer_periods_and_prices_confirmed_in_real_config(real_production_config):
    from app.deps import PROJECT_ROOT
    from app.pricing.provider import available_periods
    from app.settings import load_settings

    settings = load_settings(PROJECT_ROOT)
    periods = {p.code: p for p in available_periods(settings, "GE", "trailer")}
    assert list(periods.keys()) == ["15d", "30d", "90d"]
    assert periods["15d"].price_rub == 849
    assert periods["30d"].price_rub == 1249
    assert periods["90d"].price_rub == 1799
    assert "1y" not in periods


# --------------------------- B: category UI -----------------------------------


def test_trailer_category_becomes_selectable_with_its_three_periods_no_1y(real_production_config):
    real_client = TestClient(app)
    response = real_client.get("/api/periods", params={"category_code": "trailer"})
    assert response.status_code == 200
    body = response.json()
    assert [p["code"] for p in body] == ["15d", "30d", "90d"]
    assert [p["price_rub"] for p in body] == [849, 1249, 1799]

    response = real_client.post(
        "/category-period", data={"category_code": "trailer", "period_code": "30d"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"

    response = real_client.get("/category-period")
    assert "1 год" not in response.text
    assert 'data-period-code="1y"' not in response.text


# --------------------------- C+D: full manual flow via CHASSIS -----------------


def test_full_manual_flow_trailer_via_chassis_reaches_summary_with_correct_category_and_price(
    real_production_config,
):
    """Chassis (not VIN) is the path that matters most for trailer in
    practice -- this exercises it end to end rather than defaulting to VIN
    like most other flow tests in this suite."""
    client_ = TestClient(app)

    response = client_.post(
        "/category-period", data={"category_code": "trailer", "period_code": "15d"}, follow_redirects=False
    )
    assert response.status_code == 303

    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "TR001AA",
            "identifier_type": "chassis",
            "identifier": "CHS0001TRAILER",
            "manufacturer_id": str(_trailer_manufacturer_id),
            "model_id": str(_trailer_model_id),
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
    assert order.vehicle_category_code == "trailer"
    assert order.period_code == "15d"
    assert order.price_customer_minor == 849 * 100
    assert order.identifier_type == "chassis"
    assert order.identifier == "CHS0001TRAILER"

    response = client_.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200
    assert "Прицеп" in response.text
    assert "849" in response.text
    assert "CHS0001TRAILER" in response.text
    assert "Номер шасси" in response.text  # chassis label, not "VIN номер"


# --------------------------- E: manufacturer/model catalog --------------------


def test_trailer_vehicle_step_uses_the_existing_global_catalog(real_production_config):
    """Not re-proving the global-catalog/no-category-restriction finding
    again (already established for motorcycle) -- just confirming trailer
    goes through the same catalog validation path successfully."""
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "trailer", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "TR002BB",
            "identifier_type": "chassis",
            "identifier": "CHS0002",
            "manufacturer_id": str(_trailer_manufacturer_id),
            "model_id": str(_trailer_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"


# --------------------------- F: documents/OCR route ---------------------------


@pytest.fixture
def fake_provider():
    holder = {"provider": FakeOcrProvider()}
    app.dependency_overrides[get_ocr_provider] = lambda: holder["provider"]
    yield holder
    app.dependency_overrides.pop(get_ocr_provider, None)


def test_trailer_documents_flow_prefills_chassis_not_vin(real_production_config, fake_provider):
    """Never a real OpenAI call -- FakeOcrProvider only. Chassis-only
    recognition (vin=None) is the realistic case for a trailer document."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="TR003CC",
            vin=None,
            chassis_number="CHS0003TRAILER",
            manufacturer="ZTRAILERFICTIONALMAKE",
            model="ZTRAILERFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "trailer", "period_code": "30d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "documents"})

    response = client_.post(
        "/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    review = client_.get("/vehicle")
    assert review.status_code == 200
    assert 'value="TR003CC"' in review.text
    assert 'value="CHS0003TRAILER"' in review.text
    assert "Номер шасси" in review.text  # chassis toggle selected, not VIN
    assert "ZTRAILERFICTIONALMAKE" in review.text

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
    assert order.vehicle_category_code == "trailer"
    assert order.identifier_type == "chassis"
    assert order.price_customer_minor == 1249 * 100  # 30d


# --------------------------- G: 1y rejection -----------------------------------


def test_trailer_1y_is_rejected_server_side_against_real_config(real_production_config):
    real_client = TestClient(app)
    response = real_client.post(
        "/category-period",
        data={"category_code": "trailer", "period_code": "1y"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text
