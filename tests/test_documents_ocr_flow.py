"""End-to-end route tests for the "Загрузить документы" OCR flow --
FakeOcrProvider only, injected via FastAPI's dependency_overrides. No test
in this file ever touches a real Vision API or a real document image (all
synthetic, tiny, in-memory JPEGs)."""

import io

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.catalog.repository import mark_models_synced, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import get_ocr_provider, get_settings
from app.main import app
from app.ocr.models import OcrResult
from app.ocr.provider import FakeOcrProvider, OcrProviderError

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
_ocr_bmw_id = upsert_manufacturer(_conn, external_id=5001, name="ZOCRFICTIONALMAKE", is_popular=False)
_ocr_model_id = upsert_model(_conn, external_id=5001, manufacturer_id=_ocr_bmw_id, name="ZOCRFICTIONALMODEL")
mark_models_synced(_conn, _ocr_bmw_id)
_conn.commit()
_conn.close()


def _make_jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (150, 20, 20)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def fake_provider():
    """holder["provider"] can be reassigned after the fixture starts (each
    test sets its own canned result/error) while the FastAPI dependency
    override always calls through to whatever is currently in the box."""
    holder = {"provider": FakeOcrProvider()}
    app.dependency_overrides[get_ocr_provider] = lambda: holder["provider"]
    yield holder
    app.dependency_overrides.pop(get_ocr_provider, None)


def _reach_documents_upload(client_):
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "documents"})


def _count_orders():
    conn = get_connection(_settings.app.db_file)
    try:
        return conn.execute("SELECT COUNT(*) FROM insurance_orders").fetchone()[0]
    finally:
        conn.close()


def _draft_data_for(client_) -> str:
    session_id = client_.cookies.get("ai_session")
    conn = get_connection(_settings.app.db_file)
    try:
        row = conn.execute(
            "SELECT draft_data FROM insurance_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    return row["draft_data"] or ""


# --------------------------- availability -----------------------------------


def test_recognition_unavailable_when_no_provider_configured():
    app.dependency_overrides[get_ocr_provider] = lambda: None
    try:
        client_ = TestClient(app)
        _reach_documents_upload(client_)
        response = client_.get("/documents-soon")
        assert response.status_code == 200
        assert "временно недоступно" in response.text
        assert 'id="upload-form"' not in response.text  # no upload form offered at all
        assert "Ввести данные вручную" in response.text  # manual fallback always present
    finally:
        app.dependency_overrides.pop(get_ocr_provider, None)


def test_upload_rejected_server_side_when_provider_unavailable_at_post_time(fake_provider):
    """Even if the page was rendered while available, POST re-checks
    availability rather than trusting stale client-side state."""
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    app.dependency_overrides[get_ocr_provider] = lambda: None
    response = client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})
    assert response.status_code == 503
    assert "временно недоступно" in response.text


# ------------------------------ success paths -------------------------------


def test_ocr_success_all_fields_redirects_to_vehicle_prefilled(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRFICTIONALMAKE",
            model="ZOCRFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post(
        "/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    review = client_.get("/vehicle")
    assert review.status_code == 200
    assert "Проверьте данные" in review.text
    assert 'value="AB123CD"' in review.text
    assert 'value="WVWZZZ1JZXW000001"' in review.text
    assert "ZOCRFICTIONALMAKE" in review.text
    assert "ZOCRFICTIONALMODEL" in review.text


def test_vin_present_chassis_null_logs_as_ocr_success_not_partial(fake_provider):
    """Regression test: vin/chassis_number are ALTERNATIVE identifiers --
    a fully-read document that uses VIN (chassis_number legitimately None)
    must log ocr_success, not ocr_partial."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRFICTIONALMAKE",
            model="ZOCRFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})

    conn = get_connection(_settings.app.db_file)
    try:
        row = conn.execute(
            "SELECT event_name FROM insurance_analytics_events WHERE event_name IN ('ocr_success', 'ocr_partial') ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["event_name"] == "ocr_success"


def test_partial_ocr_result_is_still_success_and_prefills_what_it_found(fake_provider):
    """Section 18: VIN + manufacturer found, registration/model not --
    still redirects into /vehicle, never an error state."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number=None,
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRFICTIONALMAKE",
            model=None,
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post(
        "/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    review = client_.get("/vehicle")
    assert 'value="WVWZZZ1JZXW000001"' in review.text
    assert "ZOCRFICTIONALMAKE" in review.text


def test_unmatched_manufacturer_text_shown_as_review_hint_not_silently_dropped(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin=None,
            chassis_number=None,
            manufacturer="TotallyUnknownBrandXYZ",
            model=None,
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})
    review = client_.get("/vehicle")
    assert "TotallyUnknownBrandXYZ" in review.text
    assert 'value="AB123CD"' in review.text


# ------------------------------ failure paths --------------------------------


def test_zero_field_ocr_result_stays_on_upload_screen_with_retry_and_manual_actions(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake", registration_number=None, vin=None, chassis_number=None, manufacturer=None, model=None
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})
    assert response.status_code == 422
    assert "Не удалось распознать" in response.text
    assert "Ввести данные вручную" in response.text  # never a dead end


def test_provider_failure_shows_generic_message_never_provider_details(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        error=OcrProviderError("upstream 500 from provider XYZ, request-id abc-123")
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})
    assert response.status_code == 502
    assert "Не удалось обработать документ" in response.text
    assert "request-id" not in response.text
    assert "XYZ" not in response.text


def test_unsupported_file_rejected_before_any_ocr_call(fake_provider):
    calls = []
    real_recognize = fake_provider["provider"].recognize
    fake_provider["provider"].recognize = lambda *a, **k: (calls.append(1), real_recognize(*a, **k))[1]

    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post("/documents-soon", files={"file": ("doc.txt", b"not an image", "text/plain")})
    assert response.status_code == 422
    assert not calls  # validation rejected it before the provider was ever called


def test_oversized_file_rejected_before_any_ocr_call(fake_provider):
    calls = []
    real_recognize = fake_provider["provider"].recognize
    fake_provider["provider"].recognize = lambda *a, **k: (calls.append(1), real_recognize(*a, **k))[1]

    client_ = TestClient(app)
    _reach_documents_upload(client_)
    oversized = b"\xff" * (10 * 1024 * 1024 + 1)
    response = client_.post("/documents-soon", files={"file": ("doc.jpg", oversized, "image/jpeg")})
    assert response.status_code == 422
    assert not calls


# --------------------------- data_entry_method / drafts ----------------------


def test_documents_method_survives_into_vehicle_review_header(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake", registration_number="AB123CD", vin=None, chassis_number=None, manufacturer=None, model=None
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})
    review = client_.get("/vehicle")
    assert "Проверьте данные" in review.text  # documents-flow header, not "Данные транспортного средства"


def test_manual_fallback_from_upload_screen_explicitly_sets_manual(fake_provider):
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post("/method", data={"choice": "manual"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"
    review = client_.get("/vehicle")
    assert "Данные транспортного средства" in review.text  # the manual header, not "Проверьте данные"


def test_user_correction_on_review_screen_overwrites_the_ocr_candidate(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRFICTIONALMAKE",
            model="ZOCRFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})

    response = client_.post(
        "/vehicle",
        data={
            "registration_number": "ZZ999ZZ",  # user corrects the OCR read
            "identifier_type": "vin",
            "identifier": "WVWZZZ1JZXW000001",
            "manufacturer_id": str(_ocr_bmw_id),
            "model_id": str(_ocr_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"

    draft_data = _draft_data_for(client_)
    assert "ZZ999ZZ" in draft_data
    assert "AB123CD" not in draft_data  # the original OCR-read value was overwritten, not kept


# ------------------------------ order integrity ------------------------------


def test_ocr_alone_never_creates_an_order(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRFICTIONALMAKE",
            model="ZOCRFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    before = _count_orders()
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})
    client_.get("/vehicle")
    assert _count_orders() == before  # reviewing OCR-prefilled data must not itself create an order


def test_ocr_flow_to_policyholder_creates_exactly_one_order_and_no_duplicate_on_resubmit(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRFICTIONALMAKE",
            model="ZOCRFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    before = _count_orders()
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})
    client_.post(
        "/vehicle",
        data={
            "registration_number": "AB123CD",
            "identifier_type": "vin",
            "identifier": "WVWZZZ1JZXW000001",
            "manufacturer_id": str(_ocr_bmw_id),
            "model_id": str(_ocr_model_id),
        },
    )
    response = client_.post(
        "/policyholder",
        data={"full_name": "Ivanov Ivan", "contact_type": "telegram", "contact_value": "@ivan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    assert _count_orders() == before + 1

    # Double-submit (browser back + resubmit) must not create a second order.
    client_.post("/policyholder", data={"full_name": "Ivanov Ivan", "contact_type": "telegram", "contact_value": "@ivan"})
    assert _count_orders() == before + 1

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.data_entry_method == "documents"  # preserved end to end, never overwritten to "manual"


# --------------------------------- privacy ------------------------------------


def test_no_raw_ocr_values_written_to_analytics_log(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer="ZOCRFICTIONALMAKE",
            model="ZOCRFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})

    conn = get_connection(_settings.app.db_file)
    try:
        rows = conn.execute(
            "SELECT properties FROM insurance_analytics_events "
            "WHERE event_name IN ('ocr_success', 'ocr_partial', 'ocr_failed')"
        ).fetchall()
    finally:
        conn.close()
    assert rows
    for row in rows:
        properties = row["properties"] or ""
        assert "AB123CD" not in properties
        assert "WVWZZZ1JZXW000001" not in properties
        assert "ZOCRFICTIONALMAKE" not in properties


def test_provider_error_log_never_contains_the_raised_message(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(error=OcrProviderError("secret-looking upstream detail 42"))
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})

    conn = get_connection(_settings.app.db_file)
    try:
        rows = conn.execute(
            "SELECT properties FROM insurance_analytics_events WHERE event_name = 'ocr_failed'"
        ).fetchall()
    finally:
        conn.close()
    assert rows
    for row in rows:
        assert "secret-looking upstream detail" not in (row["properties"] or "")


def test_ocr_failed_log_includes_safe_error_classification(fake_provider):
    """The route's log_event call spreads OcrProviderError.classification
    into properties -- confirms that plumbing end-to-end, not just the
    classify_error() unit tests in test_ocr_provider.py."""
    import json

    from app.ocr.provider import classify_error

    boom = openai.RateLimitError(
        "sensitive rate-limit detail", response=httpx2.Response(429, request=httpx2.Request("POST", "https://api.openai.com/v1/responses")), body=None
    )
    fake_provider["provider"] = FakeOcrProvider(
        error=OcrProviderError("transient failure after retry", classification=classify_error(boom, attempt=2))
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files={"file": ("doc.jpg", _make_jpeg_bytes(), "image/jpeg")})

    conn = get_connection(_settings.app.db_file)
    try:
        row = conn.execute(
            "SELECT properties FROM insurance_analytics_events WHERE event_name = 'ocr_failed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    properties = json.loads(row["properties"])
    assert properties["category"] == "rate_limit"
    assert properties["status_code"] == 429
    assert properties["retry_attempt"] == 2
    assert "sensitive rate-limit detail" not in row["properties"]
