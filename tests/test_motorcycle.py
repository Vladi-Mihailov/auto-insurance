"""MOTORCYCLE enablement regression tests.

tpl.ge research (2026-08-13) confirmed motorcycle reuses passenger_car's
exact checkout flow, vehicle-fields, and manufacturer/model catalog with no
category-specific code required -- these tests exercise motorcycle through
the SAME routes/templates passenger_car already uses, never a separate
motorcycle-only code path. They run against the REAL config/config.yaml
(via real_production_config, same pattern as
test_production_pricing_confirmed_for_ge_passenger_car in
test_routes_smoke.py) because the confirmed RUB tariffs (1059/1549/2899)
live there, not in tests/fixtures/test_config.yaml.
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
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
# external_id ranges chosen to not collide with test_routes_smoke.py
# (7/12/999/24376/1/3/4) or test_documents_ocr_flow.py (5001) -- all three
# files share the same file-backed test DB (see tests/conftest.py).
upsert_category(_conn, external_id=10, code="motorcycle", name="Мотоцикл", icon="motorcycle")
_moto_manufacturer_id = upsert_manufacturer(_conn, external_id=6001, name="ZMOTOFICTIONALMAKE", is_popular=True)
_moto_model_id = upsert_model(_conn, external_id=6001, manufacturer_id=_moto_manufacturer_id, name="ZMOTOFICTIONALMODEL")
# A manufacturer/model with no motorcycle-ish name at all -- stands in for
# the real tpl.ge finding (FERRARI selectable while category=Мотоцикл) that
# our shared, category-agnostic catalog is expected to reproduce exactly,
# per the explicit decision not to add category restrictions.
_car_only_manufacturer_id = upsert_manufacturer(_conn, external_id=6002, name="ZCARONLYFICTIONALMAKE", is_popular=False)
_car_only_model_id = upsert_model(_conn, external_id=6002, manufacturer_id=_car_only_manufacturer_id, name="ZCARONLYFICTIONALMODEL")
# Mark both manufacturers' models as already synced -- otherwise
# match_model_text/on-demand sync (see app.ocr.parser / app.catalog.sync)
# would try a REAL network call to tpl.ge for these fictional external_ids
# from inside the test suite.
mark_models_synced(_conn, _moto_manufacturer_id)
mark_models_synced(_conn, _car_only_manufacturer_id)
_conn.commit()
_conn.close()


@pytest.fixture
def real_production_config(monkeypatch):
    """Same pattern as test_routes_smoke.py's fixture of the same name --
    duplicated locally so this file has no import-order dependency on that
    module. get_settings() is @lru_cache'd, so the cache must be cleared on
    both sides of the env var change, not just unset."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (40, 40, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _count_orders() -> int:
    conn = get_connection(_settings.app.db_file)
    try:
        return conn.execute("SELECT COUNT(*) FROM insurance_orders").fetchone()[0]
    finally:
        conn.close()


# --------------------------- A: pricing --------------------------------------


def test_motorcycle_periods_and_prices_confirmed_in_real_config(real_production_config):
    from app.deps import PROJECT_ROOT
    from app.pricing.provider import available_periods
    from app.settings import load_settings

    settings = load_settings(PROJECT_ROOT)
    periods = {p.code: p for p in available_periods(settings, "GE", "motorcycle")}
    assert list(periods.keys()) == ["15d", "30d", "90d"]
    assert periods["15d"].price_rub == 1059
    assert periods["30d"].price_rub == 1549
    assert periods["90d"].price_rub == 2899
    assert "1y" not in periods


# --------------------------- B: category-period ------------------------------


def test_motorcycle_category_becomes_selectable_with_its_three_periods(real_production_config):
    real_client = TestClient(app)
    response = real_client.get("/api/periods", params={"category_code": "motorcycle"})
    assert response.status_code == 200
    body = response.json()
    assert [p["code"] for p in body] == ["15d", "30d", "90d"]
    assert all(p["is_priced"] for p in body)
    assert [p["price_rub"] for p in body] == [1059, 1549, 2899]


def test_motorcycle_category_period_post_succeeds_and_advances_to_date(real_production_config):
    real_client = TestClient(app)
    response = real_client.post(
        "/category-period",
        data={"category_code": "motorcycle", "period_code": "30d"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"


def test_motorcycle_category_period_screen_never_renders_a_1y_card(real_production_config):
    real_client = TestClient(app)
    real_client.post("/category-period", data={"category_code": "motorcycle", "period_code": "15d"})

    response = real_client.get("/category-period")
    assert response.status_code == 200
    assert 'value="motorcycle"' in response.text
    assert "1 год" not in response.text
    assert 'data-period-code="1y"' not in response.text


# --------------------------- C: full manual flow ------------------------------


def test_full_manual_flow_motorcycle_reaches_summary_with_correct_category_and_price(real_production_config):
    client_ = TestClient(app)

    response = client_.post(
        "/category-period", data={"category_code": "motorcycle", "period_code": "15d"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"

    response = client_.post("/date", data={"start_date": "2031-08-15"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/method"

    response = client_.post("/method", data={"choice": "manual"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    response = client_.get("/vehicle")
    assert response.status_code == 200
    assert "Данные транспортного средства" in response.text

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "MC001AA",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000001",
            "manufacturer_id": str(_moto_manufacturer_id),
            "model_id": str(_moto_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"

    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@ivan"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.endswith("/summary")
    resume_token = location.split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.vehicle_category_code == "motorcycle"
    assert order.period_code == "15d"
    assert order.price_customer_minor == 1059 * 100

    response = client_.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200
    assert "Мотоцикл" in response.text
    assert "1 059" in response.text
    assert "ZMOTOFICTIONALMAKE" in response.text


# --------------------------- D: manufacturer/model catalog --------------------


def test_motorcycle_vehicle_step_accepts_any_catalog_manufacturer_model_no_category_restriction(
    real_production_config,
):
    """Deliberate: the architecture decision was NOT to add category_id or
    an allowlist -- a manufacturer/model with no motorcycle-ish name at all
    must be just as acceptable for a motorcycle draft as it would be for
    passenger_car, because that is what tpl.ge's own catalog does (see the
    research report's FERRARI-while-Мотоцикл finding)."""
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "motorcycle", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2031-08-15"})
    client_.post("/method", data={"choice": "manual"})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "MC002BB",
            "identifier_type": "chassis",
            "identifier": "CHS0001",
            "manufacturer_id": str(_car_only_manufacturer_id),
            "model_id": str(_car_only_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"


def test_api_vehicle_models_for_motorcycle_draft_returns_the_same_global_list(real_production_config):
    """/api/vehicle-models takes only manufacturer_id -- no category
    parameter exists to pass, by design (matches tpl.ge exactly)."""
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "motorcycle", "period_code": "15d"})

    response = client_.get("/api/vehicle-models", params={"manufacturer_id": _car_only_manufacturer_id})
    assert response.status_code == 200
    body = response.json()
    assert body["synced"] is True
    assert {m["name"] for m in body["models"]} == {"ZCARONLYFICTIONALMODEL"}


# --------------------------- E: documents/OCR route ---------------------------


@pytest.fixture
def fake_provider():
    holder = {"provider": FakeOcrProvider()}
    app.dependency_overrides[get_ocr_provider] = lambda: holder["provider"]
    yield holder
    app.dependency_overrides.pop(get_ocr_provider, None)


def test_motorcycle_documents_flow_uses_fake_provider_and_returns_to_vehicle_editable(
    real_production_config, fake_provider
):
    """Never a real OpenAI call -- FakeOcrProvider only (see fake_provider
    fixture / app.dependency_overrides), same injection point every other
    OCR route test in tests/test_documents_ocr_flow.py uses."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="MC003CC",
            vin="JYARJ41E7KA000002",
            chassis_number=None,
            manufacturer="ZMOTOFICTIONALMAKE",
            model="ZMOTOFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "motorcycle", "period_code": "30d"})
    client_.post("/date", data={"start_date": "2031-08-15"})
    client_.post("/method", data={"choice": "documents"})

    response = client_.post(
        "/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))], follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    review = client_.get("/vehicle")
    assert review.status_code == 200
    assert "Проверьте данные" in review.text
    assert 'value="MC003CC"' in review.text
    assert "ZMOTOFICTIONALMAKE" in review.text
    assert "ZMOTOFICTIONALMODEL" in review.text  # editable, not locked

    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(full_name="Petrov Petr", contact_email="petr@example.com", contact_telegram="@petr"),
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
    assert order.vehicle_category_code == "motorcycle"
    assert order.data_entry_method == "documents"
    assert order.price_customer_minor == 1549 * 100  # 30d


# --------------------------- G: 1y absence / rejection ------------------------


def test_motorcycle_1y_is_rejected_server_side_against_real_config(real_production_config):
    real_client = TestClient(app)
    response = real_client.post(
        "/category-period",
        data={"category_code": "motorcycle", "period_code": "1y"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text


def test_motorcycle_1y_absent_from_api_periods(real_production_config):
    real_client = TestClient(app)
    response = real_client.get("/api/periods", params={"category_code": "motorcycle"})
    assert response.status_code == 200
    codes = [p["code"] for p in response.json()]
    assert "1y" not in codes
