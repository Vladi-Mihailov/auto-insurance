"""End-to-end route tests for the "Загрузить документы" OCR flow --
FakeOcrProvider only, injected via FastAPI's dependency_overrides. No test
in this file ever touches a real Vision API or a real document image (all
synthetic, tiny, in-memory JPEGs).

The route makes exactly ONE OcrProvider.recognize() call per upload batch,
carrying every photo in that batch (see app.ocr.provider's module
docstring) -- there is no app-level merge/conflict-reconciliation step
downstream of it (app.ocr.merge was retired for exactly this reason).
Document-aware source rules (e.g. "vehicle fields only from the vehicle
registration document, never from a passport/license/power-of-attorney
also in the batch") live entirely in the prompt (see
tests/test_ocr_provider.py's prompt-content tests) -- a FakeOcrProvider
can't exercise that judgment, only prove the app passes every image
through in one call and trusts whatever OcrResult comes back."""

import io

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.catalog.repository import mark_models_synced, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import PROJECT_ROOT, get_ocr_provider, get_settings
from app.main import app
from app.ocr.models import OcrResult
from app.ocr.provider import FakeOcrProvider, OcrProvider, OcrProviderError
from policyholder_helpers import valid_policyholder_data

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


class _CapturingOcrProvider(OcrProvider):
    """Test-only double that records how many recognize() calls happened
    and how many images each call carried -- proves the route makes ONE
    call per upload batch (never one call per photo), and lets a test
    queue up canned per-CALL results (not per-image; a real single call
    already reflects the model having combined/reconciled every image it
    was given)."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[int] = []  # one entry per recognize() call = image count in that call

    def recognize(self, images):
        self.calls.append(len(images))
        return self._results.pop(0)


def _files_payload(*named_bytes):
    return [("files", (name, data, "image/jpeg")) for name, data in named_bytes]


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
    client_.post("/date", data={"start_date": "2030-01-15"})
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
    response = client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])
    assert response.status_code == 503
    assert "временно недоступно" in response.text


# ------------------------------ camera capture button -------------------------
#
# A plain `<input type=file accept="image/*" capture="environment">` was
# tried first, but real-device QA showed `capture` is only a hint -- some
# Android Chrome / vendor browser combinations show the same
# camera-or-gallery chooser as a bare file input rather than opening the
# camera directly, so it can't guarantee a camera-first flow. The camera
# action is now a plain <button> wired to getUserMedia (see
# app/web/static/js/documents_upload.js) -- the live preview, capture-to-
# File, and stream cleanup are pure client-side/browser behavior that
# TestClient can't exercise (no DOM, no MediaDevices), so those paths are
# covered by Playwright QA against a real (or fake-device) browser instead.
# What IS meaningful to assert server-side is the rendered markup itself:
# the button/modal shape, and that the gallery input is untouched.


def test_camera_button_present_not_a_file_input(fake_provider):
    """The camera action is a <button>, not a capture-hinting file input --
    asserting the OLD `<input capture>` markup is gone is as important here
    as asserting the new button exists, since "capture attribute present"
    was exactly the false signal this fix corrects."""
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.get("/documents-soon")
    assert response.status_code == 200
    assert 'id="camera-area"' in response.text
    assert 'id="camera-input"' not in response.text
    assert "capture=" not in response.text
    assert 'id="camera-modal"' in response.text
    assert 'id="camera-video"' in response.text
    assert 'id="camera-capture"' in response.text
    assert 'id="camera-cancel"' in response.text


def test_camera_modal_has_loading_and_error_states_in_markup(fake_provider):
    """Section 4/5 of the follow-up bugfix: the modal must be able to show
    a "Открываем камеру…" loading state and a plain-language error state
    entirely from markup that already exists on page load -- these are
    toggled via a `data-state` attribute in JS (see documents_upload.js),
    never injected/created only after getUserMedia settles, which is what
    made the very first getUserMedia version look like a dead button (no
    feedback existed at all until the promise resolved)."""
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.get("/documents-soon")
    assert response.status_code == 200
    assert 'id="camera-modal" hidden data-state="loading"' in response.text
    assert 'id="camera-status-loading"' in response.text
    assert "Открываем камеру" in response.text
    assert 'id="camera-status-error"' in response.text
    assert "Не удалось открыть камеру" in response.text
    # Capture starts disabled -- only enabled once a live stream is active.
    assert 'id="camera-capture" disabled' in response.text


def test_gallery_input_unchanged_alongside_camera_button(fake_provider):
    """The existing gallery/file picker input keeps its original
    name/accept/multiple/max-files attributes -- the camera action is
    additive, not a replacement."""
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.get("/documents-soon")
    assert response.status_code == 200
    assert 'id="upload-input"' in response.text
    assert 'name="files"' in response.text
    assert 'accept="image/jpeg,image/png,image/webp"' in response.text
    assert "multiple" in response.text
    assert 'data-max-files="5"' in response.text


def test_camera_button_absent_when_recognition_unavailable():
    """When OCR is unavailable, neither the camera action nor the gallery
    action is offered -- only the manual-entry fallback."""
    app.dependency_overrides[get_ocr_provider] = lambda: None
    try:
        client_ = TestClient(app)
        _reach_documents_upload(client_)
        response = client_.get("/documents-soon")
        assert response.status_code == 200
        assert 'id="camera-area"' not in response.text
        assert 'id="upload-input"' not in response.text
    finally:
        app.dependency_overrides.pop(get_ocr_provider, None)


def test_documents_upload_js_wires_getusermedia_not_only_dom_attribute():
    """Regression guard for the exact false assumption this fix corrects:
    the JS must actually call getUserMedia (feature-detected) and drive the
    modal's data-state (loading/active/error, see documents_soon.html for
    the corresponding markup) rather than relying on `capture` alone."""
    js_source = _documents_upload_js_source()
    assert "getUserMedia" in js_source
    assert "isSecureContext" in js_source
    assert 'setCameraState("error")' in js_source
    assert "getTracks" in js_source and ".stop()" in js_source  # stream cleanup


def test_documents_upload_js_shows_modal_before_awaiting_getusermedia():
    """Regression guard for the follow-up "dead button" bug: the modal
    (cameraModal.hidden = false) must be set, and the loading state
    entered, in the SAME synchronous call as the click handler -- i.e.
    textually before the `.then(` of the getUserMedia promise chain --
    never only inside `.then()`/`.catch()`, which is what left the button
    looking completely inert while a permission prompt or slow camera
    hardware startup was still pending."""
    js_source = _documents_upload_js_source()
    open_camera_start = js_source.index("function openCamera")
    get_user_media_call = js_source.index("getUserMedia(", open_camera_start)
    modal_shown = js_source.index("cameraModal.hidden = false", open_camera_start)
    set_loading = js_source.index('setCameraState("loading")', open_camera_start)
    assert modal_shown < get_user_media_call
    assert set_loading < get_user_media_call


def test_documents_upload_js_cache_busting_version_was_bumped():
    """The <script> tag's ?v= query string must change whenever the file's
    behavior changes meaningfully -- it stayed at ?v=2 across both the
    original file-input-capture implementation AND the first getUserMedia
    rewrite, which is exactly the kind of gap that lets a browser serve a
    stale cached bundle (old JS wired to elements the new template no
    longer renders) after a template/JS pair changes together."""
    template_path = PROJECT_ROOT / "app" / "web" / "templates" / "documents_soon.html"
    html_source = template_path.read_text(encoding="utf-8")
    assert "documents_upload.js?v=2" not in html_source
    assert "documents_upload.js?v=" in html_source


def _documents_upload_js_source() -> str:
    js_path = PROJECT_ROOT / "app" / "web" / "static" / "js" / "documents_upload.js"
    return js_path.read_text(encoding="utf-8")


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
        "/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))], follow_redirects=False
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
    client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])

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
        "/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))], follow_redirects=False
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
    client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])
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
    response = client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])
    assert response.status_code == 422
    assert "Не удалось распознать" in response.text
    assert "Ввести данные вручную" in response.text  # never a dead end


def test_provider_failure_shows_generic_message_never_provider_details(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        error=OcrProviderError("upstream 500 from provider XYZ, request-id abc-123")
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])
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
    response = client_.post("/documents-soon", files=[("files", ("doc.txt", b"not an image", "text/plain"))])
    assert response.status_code == 422
    assert not calls  # validation rejected it before the provider was ever called


def test_oversized_file_rejected_before_any_ocr_call(fake_provider):
    calls = []
    real_recognize = fake_provider["provider"].recognize
    fake_provider["provider"].recognize = lambda *a, **k: (calls.append(1), real_recognize(*a, **k))[1]

    client_ = TestClient(app)
    _reach_documents_upload(client_)
    oversized = b"\xff" * (10 * 1024 * 1024 + 1)
    response = client_.post("/documents-soon", files=[("files", ("doc.jpg", oversized, "image/jpeg"))])
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
    client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])
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
    client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])

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
    client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])
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
    client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])
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
        data=valid_policyholder_data(contact_telegram="@ivan"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    assert _count_orders() == before + 1

    # Double-submit (browser back + resubmit) must not create a second order.
    client_.post("/policyholder", data=valid_policyholder_data(contact_telegram="@ivan"))
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
    client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])

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
    client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])

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
    client_.post("/documents-soon", files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))])

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


# ------------------------------ multi-document upload -------------------------


def test_web_accepts_multiple_image_files(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number=None,
            manufacturer=None,
            model=None,
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post(
        "/documents-soon",
        files=_files_payload(("front.jpg", _make_jpeg_bytes()), ("back.jpg", _make_jpeg_bytes())),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"


def test_front_and_back_of_one_document_combine_into_one_call_one_result(fake_provider):
    """Front has registration_number + VIN, back has manufacturer + model
    (as photographed) -- since the model sees both in ONE call, the
    FakeOcrProvider double stands in for "the model already combined
    them" by returning all four already populated. This proves the route
    makes exactly one recognize() call carrying both images and just uses
    its result verbatim -- there is no separate app-level merge step
    (see app.ocr.provider's module docstring for why)."""
    provider = _CapturingOcrProvider(
        [
            OcrResult(
                provider="fake",
                registration_number="AB123CD",
                vin="WVWZZZ1JZXW000001",
                chassis_number=None,
                manufacturer="ZOCRFICTIONALMAKE",
                model="ZOCRFICTIONALMODEL",
            )
        ]
    )
    fake_provider["provider"] = provider
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post(
        "/documents-soon",
        files=_files_payload(("sts_front.jpg", _make_jpeg_bytes()), ("sts_back.jpg", _make_jpeg_bytes())),
    )
    assert provider.calls == [2]  # exactly one call, carrying both images

    review = client_.get("/vehicle")
    assert "Проверьте данные" in review.text
    assert 'value="AB123CD"' in review.text
    assert 'value="WVWZZZ1JZXW000001"' in review.text
    assert "ZOCRFICTIONALMAKE" in review.text
    assert "ZOCRFICTIONALMODEL" in review.text


def test_ambiguous_vehicle_evidence_is_null_not_guessed_and_no_conflict_banner(fake_provider):
    """If the model can't confidently resolve the vehicle document's own
    evidence (e.g. two genuinely different VIN readings across photos of
    the same techpassport), it returns null for that field -- the app
    never second-guesses or flags this: there is no app-level conflict
    detection/banner anymore (see app.ocr.provider's module docstring for
    why a single multimodal call retired that mechanism). Whatever the
    provider returns is used as-is; the user can still fill in VIN by
    hand on /vehicle, same as any other unrecognized field."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin=None,  # model couldn't confidently resolve conflicting evidence
            chassis_number=None,
            manufacturer="ZOCRFICTIONALMAKE",
            model="ZOCRFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files=_files_payload(("a.jpg", _make_jpeg_bytes()), ("b.jpg", _make_jpeg_bytes())))

    review = client_.get("/vehicle")
    assert 'value="AB123CD"' in review.text
    assert "ZOCRFICTIONALMAKE" in review.text
    # No conflict-banner wording anywhere -- the feature/markup was removed,
    # not just unlucky not to trigger.
    assert "разных фото" not in review.text
    assert "конфликт" not in review.text.lower()


def test_vin_and_chassis_both_present_prefers_vin_not_treated_as_conflict(fake_provider):
    """vin and chassis_number are different fields, not alternative reads
    of "the identifier" -- both being non-null in one OcrResult (e.g. both
    visible on the registration document) is not a conflict of any kind;
    app.ocr.parser.choose_identifier's existing VIN-takes-precedence rule
    applies exactly as it always has for a single-photo read."""
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake",
            registration_number="AB123CD",
            vin="WVWZZZ1JZXW000001",
            chassis_number="CHS999999",
            manufacturer="ZOCRFICTIONALMAKE",
            model="ZOCRFICTIONALMODEL",
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    client_.post("/documents-soon", files=_files_payload(("a.jpg", _make_jpeg_bytes()), ("b.jpg", _make_jpeg_bytes())))

    review = client_.get("/vehicle")
    assert 'value="WVWZZZ1JZXW000001"' in review.text
    assert "CHS999999" not in review.text  # VIN wins, per existing choose_identifier precedence


@pytest.mark.parametrize("file_count", [1, 3, 5])
def test_model_call_count_is_exactly_one_regardless_of_batch_size(fake_provider, file_count):
    provider = _CapturingOcrProvider(
        [
            OcrResult(
                provider="fake", registration_number="AB123CD", vin="WVWZZZ1JZXW000001", chassis_number=None, manufacturer=None, model=None
            )
        ]
    )
    fake_provider["provider"] = provider
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    payload = _files_payload(*[(f"doc{i}.jpg", _make_jpeg_bytes()) for i in range(file_count)])
    response = client_.post("/documents-soon", files=payload, follow_redirects=False)
    assert response.status_code == 303
    assert provider.calls == [file_count]  # exactly one call, carrying all `file_count` images


def test_single_file_upload_still_works_with_the_multi_file_field(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake", registration_number="AB123CD", vin="WVWZZZ1JZXW000001", chassis_number=None, manufacturer=None, model=None
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post(
        "/documents-soon", files=_files_payload(("doc.jpg", _make_jpeg_bytes())), follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"


def test_invalid_file_among_multiple_rejected_server_side_before_any_ocr_call(fake_provider):
    provider = _CapturingOcrProvider([])
    fake_provider["provider"] = provider
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post(
        "/documents-soon",
        files=[
            ("files", ("good.jpg", _make_jpeg_bytes(), "image/jpeg")),
            ("files", ("bad.txt", b"not an image", "text/plain")),
        ],
    )
    assert response.status_code == 422
    assert not provider.calls  # the whole batch is rejected before any OCR call, not just the bad file


def test_too_many_files_rejected(fake_provider):
    provider = _CapturingOcrProvider([])
    fake_provider["provider"] = provider
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    payload = _files_payload(*[(f"doc{i}.jpg", _make_jpeg_bytes()) for i in range(6)])  # over MAX_FILES_PER_RECOGNITION
    response = client_.post("/documents-soon", files=payload)
    assert response.status_code == 422
    assert "Слишком много файлов" in response.text
    assert not provider.calls


def test_oversized_file_among_multiple_rejected(fake_provider):
    provider = _CapturingOcrProvider([])
    fake_provider["provider"] = provider
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    oversized = b"\xff" * (10 * 1024 * 1024 + 1)
    response = client_.post(
        "/documents-soon",
        files=[
            ("files", ("good.jpg", _make_jpeg_bytes(), "image/jpeg")),
            ("files", ("huge.jpg", oversized, "image/jpeg")),
        ],
    )
    assert response.status_code == 422
    assert not provider.calls


def test_partial_multi_document_result_still_reaches_vehicle(fake_provider):
    fake_provider["provider"] = FakeOcrProvider(
        result=OcrResult(
            provider="fake", registration_number=None, vin="WVWZZZ1JZXW000001", chassis_number=None, manufacturer=None, model=None
        )
    )
    client_ = TestClient(app)
    _reach_documents_upload(client_)
    response = client_.post(
        "/documents-soon",
        files=_files_payload(("a.jpg", _make_jpeg_bytes()), ("b.jpg", _make_jpeg_bytes())),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"
    review = client_.get("/vehicle")
    assert 'value="WVWZZZ1JZXW000001"' in review.text


def test_multi_document_analytics_logs_files_count_but_no_document_content(fake_provider):
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
    client_.post(
        "/documents-soon",
        files=_files_payload(("sts_front.jpg", _make_jpeg_bytes()), ("sts_back.jpg", _make_jpeg_bytes())),
    )

    conn = get_connection(_settings.app.db_file)
    try:
        rows = conn.execute(
            "SELECT properties FROM insurance_analytics_events WHERE event_name IN ('ocr_success', 'ocr_partial')"
        ).fetchall()
    finally:
        conn.close()
    assert rows
    row = rows[-1]
    properties = row["properties"] or ""
    assert '"files_count": 2' in properties
    assert "AB123CD" not in properties
    assert "WVWZZZ1JZXW000001" not in properties
    assert "ZOCRFICTIONALMAKE" not in properties
    assert "sts_front.jpg" not in properties
    assert "sts_back.jpg" not in properties
