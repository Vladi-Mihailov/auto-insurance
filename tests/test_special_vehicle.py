"""SPECIAL_VEHICLE enablement regression tests.

tpl.ge research (2026-08-13) confirmed special_vehicle's checkout flow,
vehicle fields, and manufacturer/model catalog are identical to every other
enabled category, and that tpl.ge itself performs no equipment-type/purpose
validation anywhere -- these tests are deliberately light: they check
special_vehicle-specific config/state wiring, not general checkout
mechanics already covered by test_routes_smoke.py / test_motorcycle.py /
test_trailer.py / test_bus.py / test_truck.py / test_documents_ocr_flow.py.

special_vehicle's confirmed RUB tariffs (1349/2149/3649) currently equal
passenger_car's numbers -- that is a coincidence of the two independent
product decisions, not a code dependency. Test B specifically proves the
pricing provider has no fallback/reference from special_vehicle to
passenger_car, using an inline test-only Settings object with DIFFERENT
values for the two categories (production config/config.yaml is never
touched by this test).
"""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import get_ocr_provider, get_settings
from app.main import app
from app.ocr.models import OcrResult
from app.ocr.provider import FakeOcrProvider
from app.pricing.provider import available_periods
from app.settings import AppSettings, PaymentSettings, PeriodConfig, PricingSettings, Settings

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
# external_id ranges chosen to not collide with test_routes_smoke.py
# (7/12/999/24376/1/3/4), test_documents_ocr_flow.py (5001),
# test_motorcycle.py (6001/6002), test_trailer.py (7001), test_bus.py
# (8001), or test_truck.py (9001) -- all share the same file-backed test DB
# (see tests/conftest.py).
upsert_category(_conn, external_id=12, code="special_vehicle", name="Спецтехника", icon="special_vehicle")
_special_manufacturer_id = upsert_manufacturer(_conn, external_id=10001, name="ZSPECIALFICTIONALMAKE", is_popular=True)
_special_model_id = upsert_model(
    _conn, external_id=10001, manufacturer_id=_special_manufacturer_id, name="ZSPECIALFICTIONALMODEL"
)
# Mark models synced -- otherwise match_model_text/on-demand sync (see
# app.ocr.parser / app.catalog.sync) would try a REAL network call to
# tpl.ge for this fictional external_id from inside the test suite.
mark_models_synced(_conn, _special_manufacturer_id)
_conn.commit()
_conn.close()


@pytest.fixture
def real_production_config(monkeypatch):
    """Same pattern as test_routes_smoke.py / test_motorcycle.py /
    test_trailer.py / test_bus.py / test_truck.py -- reads the confirmed
    special_vehicle tariffs from the real config/config.yaml."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (120, 120, 120)).save(buffer, format="JPEG")
    return buffer.getvalue()


# --------------------------- A: pricing (own config block) -------------------


def test_special_vehicle_periods_and_prices_confirmed_in_real_config(real_production_config):
    from app.deps import PROJECT_ROOT
    from app.settings import load_settings

    settings = load_settings(PROJECT_ROOT)
    periods = {p.code: p for p in available_periods(settings, "GE", "special_vehicle")}
    assert list(periods.keys()) == ["15d", "30d", "90d"]
    assert periods["15d"].price_rub == 1349
    assert periods["30d"].price_rub == 2149
    assert periods["90d"].price_rub == 3649
    assert "1y" not in periods

    # A distinct config key exists for special_vehicle -- not merely reusing
    # passenger_car's block under another name.
    assert "special_vehicle" in settings.pricing.periods_by_country_category["GE"]


# --------------------------- B: independence regression -----------------------


def test_special_vehicle_pricing_has_no_fallback_to_passenger_car():
    """Proves the lookup itself has no category fallback -- built with an
    inline test-only Settings where passenger_car and special_vehicle have
    DELIBERATELY DIFFERENT values, so a fallback/shared-reference bug would
    make this fail. Production config/config.yaml is never touched here."""
    settings = Settings(
        app=AppSettings(db_file=Path("unused.db")),
        pricing=PricingSettings(
            periods_by_country_category={
                "GE": {
                    "passenger_car": [PeriodConfig(code="15d", label="15 дней", price_rub=111)],
                    "special_vehicle": [PeriodConfig(code="15d", label="15 дней", price_rub=222)],
                }
            }
        ),
        payment=PaymentSettings(bank_name="Bank", card_number="0000", card_holder="X"),
    )

    special = available_periods(settings, "GE", "special_vehicle")
    passenger = available_periods(settings, "GE", "passenger_car")
    assert special[0].price_rub == 222
    assert passenger[0].price_rub == 111
    assert special[0].price_rub != passenger[0].price_rub


# --------------------------- C: category UI -----------------------------------


def test_special_vehicle_category_becomes_selectable_with_its_three_periods_no_1y(real_production_config):
    real_client = TestClient(app)
    response = real_client.get("/api/periods", params={"category_code": "special_vehicle"})
    assert response.status_code == 200
    body = response.json()
    assert [p["code"] for p in body] == ["15d", "30d", "90d"]
    assert [p["price_rub"] for p in body] == [1349, 2149, 3649]

    response = real_client.post(
        "/category-period",
        data={"category_code": "special_vehicle", "period_code": "30d"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"

    response = real_client.get("/category-period")
    assert "1 349" in response.text
    assert "2 149" in response.text
    assert "3 649" in response.text
    assert "1 год" not in response.text
    assert 'data-period-code="1y"' not in response.text


# --------------------------- D: full manual flow ------------------------------


def test_full_manual_flow_special_vehicle_reaches_summary_with_correct_category_and_price(real_production_config):
    client_ = TestClient(app)

    response = client_.post(
        "/category-period",
        data={"category_code": "special_vehicle", "period_code": "15d"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "SPEC001AA",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000501",
            "manufacturer_id": str(_special_manufacturer_id),
            "model_id": str(_special_model_id),
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
    assert order.vehicle_category_code == "special_vehicle"
    assert order.period_code == "15d"
    assert order.price_customer_minor == 134900

    response = client_.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200
    assert "Спецтехника" in response.text
    assert "1 349" in response.text


# --------------------------- E: manufacturer/model catalog --------------------


def test_special_vehicle_vehicle_step_uses_the_existing_global_catalog(real_production_config):
    """Not re-proving the global-catalog/no-category-restriction finding
    again (already established for every prior category) -- just
    confirming special_vehicle goes through the same catalog validation
    path successfully."""
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "special_vehicle", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "SPEC002BB",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000502",
            "manufacturer_id": str(_special_manufacturer_id),
            "model_id": str(_special_model_id),
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


def test_special_vehicle_documents_flow_uses_the_existing_pipeline_with_no_special_branching(
    real_production_config, fake_provider
):
    """Never a real OpenAI call -- FakeOcrProvider only."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="SPEC003CC",
            vin="JYARJ41E7KA000503",
            chassis_number=None,
            manufacturer="ZSPECIALFICTIONALMAKE",
            model="ZSPECIALFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "special_vehicle", "period_code": "90d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "documents"})

    response = client_.post(
        "/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    review = client_.get("/vehicle")
    assert review.status_code == 200
    assert 'value="SPEC003CC"' in review.text
    assert "ZSPECIALFICTIONALMAKE" in review.text

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
    assert order.vehicle_category_code == "special_vehicle"
    assert order.price_customer_minor == 3649 * 100  # 90d


# --------------------------- G: 1y rejection -----------------------------------


def test_special_vehicle_1y_is_rejected_server_side_against_real_config(real_production_config):
    real_client = TestClient(app)
    response = real_client.post(
        "/category-period",
        data={"category_code": "special_vehicle", "period_code": "1y"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text


# --------------------------- H: all-category regression -----------------------


def test_all_categories_remain_correctly_priced_and_1y_free_against_real_config(real_production_config):
    from app.deps import PROJECT_ROOT
    from app.settings import load_settings

    settings = load_settings(PROJECT_ROOT)

    expected = {
        "passenger_car": {"15d": 1349, "30d": 2149, "90d": 3649},
        "motorcycle": {"15d": 1059, "30d": 1549, "90d": 2899},
        "trailer": {"15d": 849, "30d": 1249, "90d": 1799},
        "bus": {"15d": 1999, "30d": 3199, "90d": 5449},
        "truck": {"15d": 2699, "30d": 4299, "90d": 7299},
        "special_vehicle": {"15d": 1349, "30d": 2149, "90d": 3649},
    }
    for category, prices in expected.items():
        periods = {p.code: p.price_rub for p in available_periods(settings, "GE", category)}
        assert periods == prices, category
