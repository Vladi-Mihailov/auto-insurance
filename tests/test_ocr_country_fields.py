"""Step 5 of the GE/AM/TR rollout: OCR autofill for engine_power/model_year/
date_of_birth. Same FakeOcrProvider-injection pattern as
tests/test_documents_ocr_flow.py -- no test here touches a real Vision API.

Every OCR-read value for these three fields goes through the EXACT SAME
Step 4 validators manual entry uses (see
app.web.checkout_routes._ocr_engine_power_or_none/_ocr_model_year_or_none/
_ocr_date_of_birth_or_none) before it's allowed to autofill anything -- an
implausible OCR read is silently dropped, never surfaced as an error, and
never blocks the rest of the OCR result from prefilling normally.
"""

import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import SESSION_COOKIE_NAME, get_ocr_provider, get_settings
from app.main import app
from app.ocr.models import OcrResult
from app.ocr.provider import FakeOcrProvider
from app.orders.repository import get_order_by_token
from app.sessions.repository import get_draft, merge_draft
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()

# Own catalog rows -- external_id=18001 continues the per-file numbering
# convention (see tests/test_country_routing.py's own comment).
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=18001, name="ZOCRCOUNTRYFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=18001, manufacturer_id=_manufacturer_id, name="ZOCRCOUNTRYFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()

_START = today_in_georgia() + timedelta(days=120)


@pytest.fixture
def real_config(monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_provider():
    holder = {"provider": FakeOcrProvider()}
    app.dependency_overrides[get_ocr_provider] = lambda: holder["provider"]
    yield holder
    app.dependency_overrides.pop(get_ocr_provider, None)


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


def _seed_draft(client: TestClient, **updates) -> None:
    conn = get_connection(_settings.app.db_file)
    try:
        merge_draft(conn, _session_id(client), updates)
    finally:
        conn.close()


def _order(resume_token: str):
    conn = get_connection(_settings.app.db_file)
    try:
        return get_order_by_token(conn, resume_token)
    finally:
        conn.close()


def _make_jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (150, 20, 20)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _upload(client: TestClient):
    return client.post(
        "/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))], follow_redirects=False
    )


def _reach_am_documents_upload(client: TestClient) -> None:
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    client.post("/date", data={"start_date": _START.isoformat(), "end_date": (_START + timedelta(days=10)).isoformat()})
    client.post("/method", data={"choice": "documents"})


def _reach_tr_documents_upload(client: TestClient) -> None:
    """TR periods are unpriced (see tests/test_country_periods.py) -- draft
    is seeded directly, same technique used throughout this rollout."""
    _start(client, "TR")
    _seed_draft(
        client,
        vehicle_category_code="passenger_car",
        period_code="30d",
        price_customer_minor=500000,
        start_date=_START.isoformat(),
        end_date=(_START + timedelta(days=30)).isoformat(),
        data_entry_method="documents",
    )


# ---------------------------------------------------------------------------
# OCR schema: existing fields still parse, new fields parse, all nullable
# ---------------------------------------------------------------------------


def test_ocr_result_accepts_all_three_new_fields_together_with_existing_ones():
    result = OcrResult(
        provider="fake",
        registration_number="AB123CD",
        vin="WVWZZZ1JZXW000001",
        chassis_number=None,
        manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
        model="ZOCRCOUNTRYFICTIONALMODEL",
        engine_power=150,
        model_year=2020,
        date_of_birth="1990-05-20",
    )
    assert result.engine_power == 150
    assert result.model_year == 2020
    assert result.date_of_birth == "1990-05-20"
    assert result.is_complete_for_checkout is True  # unaffected by the new fields


def test_ocr_result_all_three_new_fields_can_be_null():
    result = OcrResult(
        provider="fake",
        registration_number="AB123CD",
        vin="WVWZZZ1JZXW000001",
        chassis_number=None,
        manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
        model="ZOCRCOUNTRYFICTIONALMODEL",
    )
    assert result.engine_power is None
    assert result.model_year is None
    assert result.date_of_birth is None
    assert result.is_complete_for_checkout is True  # a complete vehicle read even with all three missing


def test_fields_found_count_includes_the_three_new_fields():
    without = OcrResult(
        provider="fake", registration_number=None, vin=None, chassis_number=None, manufacturer=None, model=None
    )
    with_all_three = OcrResult(
        provider="fake",
        registration_number=None,
        vin=None,
        chassis_number=None,
        manufacturer=None,
        model=None,
        engine_power=150,
        model_year=2020,
        date_of_birth="1990-05-20",
    )
    assert without.fields_found_count == 0
    assert with_all_three.fields_found_count == 3


def test_completion_rule_unaffected_by_new_fields_being_present_or_absent():
    """Section 10: engine_power/model_year/date_of_birth must NOT become
    part of the vehicle-recognition completion criterion -- a document
    missing registration_number is still incomplete even with all three
    new fields present."""
    result = OcrResult(
        provider="fake",
        registration_number=None,  # still missing
        vin="WVWZZZ1JZXW000001",
        chassis_number=None,
        manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
        model="ZOCRCOUNTRYFICTIONALMODEL",
        engine_power=150,
        model_year=2020,
        date_of_birth="1990-05-20",
    )
    assert result.is_complete_for_checkout is False


# ---------------------------------------------------------------------------
# Armenia: engine_power autofill
# ---------------------------------------------------------------------------


def test_am_valid_engine_power_autofills_vehicle_form(real_config, fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
            engine_power=180,
        )
    )
    client = TestClient(app)
    _reach_am_documents_upload(client)
    response = _upload(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    draft = _read_draft(client)
    assert draft["engine_power"] == 180

    review = client.get("/vehicle")
    assert 'value="180"' in review.text


def test_am_missing_engine_power_leaves_manual_field_available(real_config, fake_provider):
    """Section 9: the rest of the OCR result must still prefill normally --
    only the one missing new field falls back to manual entry."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
            engine_power=None,
        )
    )
    client = TestClient(app)
    _reach_am_documents_upload(client)
    response = _upload(client)
    assert response.status_code == 303

    draft = _read_draft(client)
    assert draft["engine_power"] is None
    assert draft["registration_number"] == "AB123CD"  # rest of the result still prefilled

    review = client.get("/vehicle")
    assert review.status_code == 200
    assert 'name="engine_power"' in review.text  # field present, empty, ready for manual entry
    assert 'value="AB123CD"' in review.text


# ---------------------------------------------------------------------------
# Turkey: engine_power + model_year + date_of_birth autofill
# ---------------------------------------------------------------------------


def test_tr_valid_engine_power_and_model_year_autofill_vehicle_form(real_config, fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="TR123CD",
            vin="WVWZZZ1JZXW000002",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
            engine_power=150,
            model_year=2020,
        )
    )
    client = TestClient(app)
    _reach_tr_documents_upload(client)
    response = _upload(client)
    assert response.status_code == 303

    draft = _read_draft(client)
    assert draft["engine_power"] == 150
    assert draft["model_year"] == 2020

    review = client.get("/vehicle")
    assert 'value="150"' in review.text
    assert 'value="2020"' in review.text


def test_tr_valid_date_of_birth_autofills_policyholder_form(real_config, fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="TR123CD",
            vin="WVWZZZ1JZXW000002",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
            engine_power=150,
            model_year=2020,
            date_of_birth="1990-05-20",
        )
    )
    client = TestClient(app)
    _reach_tr_documents_upload(client)
    _upload(client)

    draft = _read_draft(client)
    assert draft["ocr_date_of_birth"] == "1990-05-20"

    client.post("/vehicle", data={
        "registration_number": "TR123CD", "identifier_type": "vin", "identifier": "WVWZZZ1JZXW000002",
        "manufacturer_id": str(_manufacturer_id), "model_id": str(_model_id),
        "engine_power": "150", "model_year": "2020",
    })
    policyholder = client.get("/policyholder")
    assert policyholder.status_code == 200
    assert 'value="1990-05-20"' in policyholder.text


def test_tr_missing_model_year_does_not_discard_other_ocr_values(real_config, fake_provider):
    """Section 9's exact example: registration_number/VIN/manufacturer/
    model/engine_power/date_of_birth all found, model_year alone missing --
    everything else must still survive; model_year falls back to manual."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="TR123CD",
            vin="WVWZZZ1JZXW000002",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
            engine_power=150,
            model_year=None,
            date_of_birth="1990-05-20",
        )
    )
    client = TestClient(app)
    _reach_tr_documents_upload(client)
    response = _upload(client)
    assert response.status_code == 303

    draft = _read_draft(client)
    assert draft["registration_number"] == "TR123CD"
    assert draft["engine_power"] == 150
    assert draft["model_year"] is None
    assert draft["ocr_date_of_birth"] == "1990-05-20"

    review = client.get("/vehicle")
    assert 'name="model_year"' in review.text  # still offered, just empty


# ---------------------------------------------------------------------------
# Validation: an implausible OCR value is never accepted as a valid autofill
# ---------------------------------------------------------------------------


def test_invalid_ocr_engine_power_zero_not_accepted_as_autofill(real_config, fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
            engine_power=0,
        )
    )
    client = TestClient(app)
    _reach_am_documents_upload(client)
    _upload(client)
    draft = _read_draft(client)
    assert draft["engine_power"] is None


def test_invalid_ocr_model_year_far_future_not_accepted_as_autofill(real_config, fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="TR123CD",
            vin="WVWZZZ1JZXW000002",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
            engine_power=150,
            model_year=3026,
        )
    )
    client = TestClient(app)
    _reach_tr_documents_upload(client)
    _upload(client)
    draft = _read_draft(client)
    assert draft["model_year"] is None
    assert draft["engine_power"] == 150  # sibling field unaffected


def test_future_ocr_date_of_birth_not_accepted_as_autofill(real_config, fake_provider):
    future_dob = (today_in_georgia() + timedelta(days=1)).isoformat()
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="TR123CD",
            vin="WVWZZZ1JZXW000002",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
            engine_power=150,
            model_year=2020,
            date_of_birth=future_dob,
        )
    )
    client = TestClient(app)
    _reach_tr_documents_upload(client)
    _upload(client)
    draft = _read_draft(client)
    assert draft.get("ocr_date_of_birth") is None


# ---------------------------------------------------------------------------
# Georgia regression
# ---------------------------------------------------------------------------


def test_ge_ocr_happy_path_unchanged():
    app.dependency_overrides[get_ocr_provider] = lambda: FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
        )
    )
    try:
        client = TestClient(app)
        _start(client, "GE")
        client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
        client.post("/date", data={"start_date": _START.isoformat()})
        client.post("/method", data={"choice": "documents"})
        response = _upload(client)
        assert response.status_code == 303
        assert response.headers["location"] == "/vehicle"
        review = client.get("/vehicle")
        assert 'value="AB123CD"' in review.text
    finally:
        app.dependency_overrides.pop(get_ocr_provider, None)


def test_ge_new_ocr_fields_do_not_appear_in_ge_vehicle_or_policyholder_ui(real_config, fake_provider):
    """Even though OCR is country-agnostic and may technically return
    engine_power/model_year/date_of_birth for a GE session (the model isn't
    told the country), GE's checkout never renders or requires them -- see
    _requires_engine_power/_requires_model_year/_requires_date_of_birth."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
            engine_power=150,
            model_year=2020,
            date_of_birth="1990-05-20",
        )
    )
    client = TestClient(app)
    _start(client, "GE")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client.post("/date", data={"start_date": _START.isoformat()})
    client.post("/method", data={"choice": "documents"})
    _upload(client)

    vehicle = client.get("/vehicle")
    assert 'name="engine_power"' not in vehicle.text
    assert 'name="model_year"' not in vehicle.text

    client.post("/vehicle", data={
        "registration_number": "AB123CD", "identifier_type": "vin", "identifier": "WVWZZZ1JZXW000001",
        "manufacturer_id": str(_manufacturer_id), "model_id": str(_model_id),
    })
    policyholder = client.get("/policyholder")
    assert 'name="date_of_birth"' not in policyholder.text

    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@ge_ocr_fields"), follow_redirects=False
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    order = _order(resume_token)
    assert order.engine_power is None
    assert order.model_year is None
    assert order.date_of_birth is None


def test_ge_ocr_completion_rule_and_success_event_unchanged(real_config, fake_provider):
    """Section 10: adding the three new fields must not change what counts
    as ocr_success vs ocr_partial for the existing GE vehicle-recognition
    flow."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRCOUNTRYFICTIONALMAKE",
            model="ZOCRCOUNTRYFICTIONALMODEL",
        )
    )
    client = TestClient(app)
    _start(client, "GE")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client.post("/date", data={"start_date": _START.isoformat()})
    client.post("/method", data={"choice": "documents"})
    _upload(client)

    conn = get_connection(_settings.app.db_file)
    try:
        row = conn.execute(
            "SELECT event_name FROM insurance_analytics_events "
            "WHERE event_name IN ('ocr_success', 'ocr_partial') ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["event_name"] == "ocr_success"
