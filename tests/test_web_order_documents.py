"""Website applications: the customer's uploaded document photos are stored
privately, attached to the created order, and delivered -- order card, then
each document -- to every active staff member of the order country's bot,
through the same outbox as Telegram orders (app.orders.web_documents,
app.telegram_bot.outbox). Real config.yaml (bot profiles), a tmp DB and tmp
upload storage per test; fake OCR; fake Bot API. Nothing leaves the test."""

import io
import os
import stat
import sys
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import SendDocument, SendMessage
from fastapi.testclient import TestClient
from PIL import Image
from policyholder_helpers import valid_policyholder_data
from telegram_bot_helpers import BotHarness, make_outbox_due, seed_catalog, sent_to

from app.checkout import service as checkout_service
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import PROJECT_ROOT, SESSION_COOKIE_NAME, get_ocr_provider, get_settings
from app.main import app
from app.notifications import bot_outbox
from app.ocr.models import OcrResult
from app.ocr.provider import FakeOcrProvider, OcrProviderError
from app.orders import files as order_files
from app.orders import web_documents
from app.orders.repository import get_order_by_token
from app.sessions.repository import get_draft
from app.settings import load_settings
from app.telegram_bot.profile import BotProfile

OWNER, MANAGER = 5712994689, 410811386  # the production staff shape: one owner + one manager
VIN = "WVWZZZ1JZXW000001"
_START = today_in_georgia() + timedelta(days=15)
_OCR = OcrResult(
    provider="fake", registration_number="AB123CD", vin=VIN, chassis_number=None, manufacturer="Toyota", model="Camry"
)


class RecordingOcr(FakeOcrProvider):
    def __init__(self, result=_OCR, error=None):
        super().__init__(result, error)
        self.batches: list[list[bytes]] = []

    def recognize(self, images):
        self.batches.append([data for data, _mime in images])
        return super().recognize(images)


def _jpeg(color) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(buffer, format="JPEG")
    return buffer.getvalue()


PHOTOS = [_jpeg((200, 10, 10)), _jpeg((10, 200, 10)), _jpeg((10, 10, 200))]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "web.db"))
    get_settings.cache_clear()
    loaded = load_settings(PROJECT_ROOT)
    yield loaded
    get_settings.cache_clear()


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


@pytest.fixture
def ocr():
    provider = RecordingOcr()
    app.dependency_overrides[get_ocr_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ocr_provider, None)


def _profile(settings, bot_key):
    return BotProfile.from_config(bot_key, settings.telegram_bot_profiles[bot_key])


@pytest.fixture
def ge_bot(settings, ids):
    """@OSAGO24GEbot's process: bootstraps its staff (owner + manager) and delivers the outbox."""
    harness = BotHarness(settings, profile=_profile(settings, "osago24ge"), manager_ids={MANAGER}, owner_id=OWNER)
    yield harness
    harness.close()


def _conn(settings):
    return get_connection(settings.app.db_file)


def _upload(client, photos=PHOTOS):
    return client.post(
        "/documents-soon",
        files=[("files", (f"IMG_{n}.jpg", data, "image/jpeg")) for n, data in enumerate(photos, start=1)],
        follow_redirects=False,
    )


def _to_upload(client, country="GE", period="30d"):
    client.get("/start", params={"country": country}, follow_redirects=False)
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": period})
    client.post("/date", data={"start_date": _START.isoformat()})
    client.post("/method", data={"choice": "documents"})


def _vehicle(client, ids, **extra):
    data = {
        "registration_number": "AB123CD", "identifier_type": "vin", "identifier": VIN,
        "manufacturer_id": str(ids["TOYOTA"]), "model_id": str(ids["TOYOTA/CAMRY"]), **extra,
    }
    return client.post("/vehicle", data=data, follow_redirects=False)


def _submit(client, **policyholder):
    response = client.post("/policyholder", data=valid_policyholder_data(**policyholder), follow_redirects=False)
    assert response.status_code == 303, response.text[:300]
    return response.headers["location"].split("/")[2]


def _web_order(settings, ids, *, upload=True, country="GE"):
    client = TestClient(app)
    _to_upload(client, country=country, period="30d")
    if upload:
        assert _upload(client).status_code == 303
    else:
        client.post("/method", data={"choice": "manual"})
    _vehicle(client, ids)
    token = _submit(client)
    conn = _conn(settings)
    try:
        return client, get_order_by_token(conn, token)
    finally:
        conn.close()


def _draft(settings, client):
    conn = _conn(settings)
    try:
        return get_draft(conn, client.cookies.get(SESSION_COOKIE_NAME)) or {}
    finally:
        conn.close()


def _files(settings, order_id):
    conn = _conn(settings)
    try:
        return order_files.list_files(conn, order_id)
    finally:
        conn.close()


def _jobs(settings, order_id):
    conn = _conn(settings)
    try:
        return bot_outbox.list_jobs(conn, order_id)
    finally:
        conn.close()


def _deliver(harness):
    make_outbox_due(harness.dispatcher["settings"].app.db_file)
    start = len(harness.session.calls)
    conn = get_connection(harness.dispatcher["settings"].app.db_file)
    try:
        sent = harness.loop.run_until_complete(harness.dispatcher["outbox"].process(harness.bot, conn))
    finally:
        conn.close()
    return sent, harness.session.calls[start:]


# ------------------------------------------------------------- A: storage


def test_upload_persists_the_originals_privately_and_ocr_still_gets_all_three(settings, ids, ocr):
    client = TestClient(app)
    _to_upload(client)
    assert _upload(client).status_code == 303
    assert len(ocr.batches) == 1 and len(ocr.batches[0]) == 3
    documents = _draft(settings, client)[web_documents.DRAFT_KEY]
    assert [d["position"] for d in documents] == [1, 2, 3]
    assert [d["original_filename"] for d in documents] == ["IMG_1.jpg", "IMG_2.jpg", "IMG_3.jpg"]
    storage = settings.app.uploads_dir
    for meta, original in zip(documents, PHOTOS):
        assert meta["stored_name"] != meta["original_filename"] and meta["stored_name"].endswith(".jpg")
        assert (storage / meta["stored_name"]).read_bytes() == original  # the ORIGINAL bytes
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(storage / meta["stored_name"]).st_mode) == 0o600
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(storage).st_mode) == 0o700
    static_dir = PROJECT_ROOT / "app" / "web" / "static"
    assert static_dir not in storage.parents  # never publicly served
    assert client.get(f"/static/{documents[0]['stored_name']}").status_code == 404


def test_order_creation_attaches_exactly_the_three_documents_before_the_draft_is_cleared(settings, ids, ocr, monkeypatch):
    from app.web import checkout_routes

    attached_at_clear = []
    real_clear = checkout_routes.clear_draft

    def spy_clear(conn, session_id):
        row = conn.execute("SELECT COUNT(*) FROM insurance_order_files").fetchone()[0]
        attached_at_clear.append(row)
        return real_clear(conn, session_id)

    monkeypatch.setattr(checkout_routes, "clear_draft", spy_clear)
    client, order = _web_order(settings, ids)
    assert attached_at_clear == [3]  # files were attached BEFORE the draft went away
    files = _files(settings, order.id)
    assert [(f.kind, f.source, f.position, f.telegram_media_type) for f in files] == [
        ("vehicle_document", "web", n, "document") for n in (1, 2, 3)
    ]
    assert all(f.storage_path and f.telegram_file_id == "" and f.bot_key == "osago24ge" for f in files)
    assert web_documents.DRAFT_KEY not in _draft(settings, client)


# ------------------------------------------------------ B/C/D: delivery


def test_owner_and_manager_each_get_the_card_and_three_numbered_documents_once(settings, ids, ocr, ge_bot):
    _client, order = _web_order(settings, ids)
    jobs = _jobs(settings, order.id)
    assert len(jobs) == 8 and len({j.dedupe_key for j in jobs}) == 8
    assert {j.bot_key for j in jobs} == {"osago24ge"}
    sent, calls = _deliver(ge_bot)
    assert sent == 8
    for staff_id in (OWNER, MANAGER):
        (card,) = sent_to(calls, staff_id, SendMessage)
        assert card.text.startswith(f"📝 Новая заявка {order.public_number} — ожидает оплаты")
        assert "Клиент: заявка с сайта" in card.text and card.reply_markup is None
        documents = sent_to(calls, staff_id, SendDocument)
        assert [d.caption for d in documents] == [
            f"📎 {order.public_number}\nДокумент клиента {n} из 3 (техпаспорт / паспорт)" for n in (1, 2, 3)
        ]
    # uploaded from storage once; the second recipient gets Telegram's copy
    first, second = sorted((OWNER, MANAGER), key=lambda s: calls.index(sent_to(calls, s, SendDocument)[0]))
    assert all(not isinstance(d.document, str) for d in sent_to(calls, first, SendDocument))
    assert all(isinstance(d.document, str) and d.document.startswith("uploaded-") for d in sent_to(calls, second, SendDocument))
    assert all(f.telegram_file_id.startswith("uploaded-") for f in _files(settings, order.id))


def test_restart_sends_nothing_twice(settings, ids, ocr, ge_bot):
    _web_order(settings, ids)
    assert _deliver(ge_bot)[0] == 8
    restarted = BotHarness(settings, profile=_profile(settings, "osago24ge"), manager_ids={MANAGER}, owner_id=OWNER)
    try:
        sent, calls = _deliver(restarted)
    finally:
        restarted.close()
    assert sent == 0 and calls == []


def test_one_failed_document_is_retried_alone(settings, ids, ocr, ge_bot):
    real = ge_bot.session.make_request
    failures = {"left": 1}

    async def flaky(bot, method, timeout=None):
        if isinstance(method, SendDocument) and "2 из 3" in (method.caption or "") and method.chat_id == OWNER and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("simulated Telegram outage")
        return await real(bot, method, timeout)

    ge_bot.session.make_request = flaky
    _client, order = _web_order(settings, ids)
    sent, _calls = _deliver(ge_bot)
    assert sent == 7
    pending = [j for j in _jobs(settings, order.id) if j.status == "pending"]
    assert len(pending) == 1 and pending[0].target_chat_id == OWNER and pending[0].attempts == 1
    sent, retried = _deliver(ge_bot)
    assert sent == 1
    assert [d.caption.splitlines()[1] for d in sent_to(retried, OWNER, SendDocument)] == ["Документ клиента 2 из 3 (техпаспорт / паспорт)"]
    assert sent_to(retried, OWNER, SendMessage) == [] and sent_to(retried, MANAGER) == []


# ---------------------------------------------- E/F: payment claim, resubmits


def test_payment_claim_does_not_repeat_the_card_or_documents(settings, ids, ocr, ge_bot):
    client, order = _web_order(settings, ids)
    _deliver(ge_bot)
    client.post(f"/o/{order.resume_token}/summary", data={"action": "confirm"}, follow_redirects=False)
    client.post(f"/o/{order.resume_token}/confirm-payment", follow_redirects=False)
    conn = _conn(settings)
    try:
        assert get_order_by_token(conn, order.resume_token).status == "payment_review"  # the claim happened
    finally:
        conn.close()
    assert len(_jobs(settings, order.id)) == 8  # nothing new queued
    assert _deliver(ge_bot) == (0, [])


def test_duplicate_submission_creates_no_duplicate_rows_or_jobs(settings, ids, ocr, ge_bot):
    client, order = _web_order(settings, ids)
    again = client.post("/policyholder", data=valid_policyholder_data(), follow_redirects=False)
    assert again.status_code == 303 and "/summary" not in again.headers["location"]  # no draft -> no second order
    conn = _conn(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM insurance_orders").fetchone()[0] == 1
        # replaying the attach step itself is a no-op too
        assert web_documents.attach_and_notify(conn, settings, order, {web_documents.DRAFT_KEY: []}) == []
    finally:
        conn.close()
    assert len(_files(settings, order.id)) == 3 and len(_jobs(settings, order.id)) == 8


def test_a_new_upload_batch_replaces_the_previous_one(settings, ids, ocr):
    client = TestClient(app)
    _to_upload(client)
    _upload(client)
    first = {d["stored_name"] for d in _draft(settings, client)[web_documents.DRAFT_KEY]}
    _upload(client, photos=PHOTOS[:2])
    second = {d["stored_name"] for d in _draft(settings, client)[web_documents.DRAFT_KEY]}
    assert len(second) == 2 and not first & second
    assert {p.name for p in settings.app.uploads_dir.iterdir()} == second  # the old batch is gone from disk


# ------------------------------------------------------ G: failures/cleanup


def test_unrecognized_upload_leaves_no_stored_files(settings, ids):
    provider = RecordingOcr(error=OcrProviderError("down", classification={}))
    app.dependency_overrides[get_ocr_provider] = lambda: provider
    try:
        client = TestClient(app)
        _to_upload(client)
        assert _upload(client).status_code == 502
    finally:
        app.dependency_overrides.pop(get_ocr_provider, None)
    assert len(provider.batches) == 1  # OCR really ran (and failed)
    assert settings.app.uploads_dir.is_dir() and list(settings.app.uploads_dir.iterdir()) == []
    assert web_documents.DRAFT_KEY not in _draft(settings, client)


def test_failed_order_finalization_leaves_no_rows_and_the_files_are_cleaned_up_later(settings, ids, ocr, monkeypatch):
    client = TestClient(app)
    _to_upload(client)
    _upload(client)
    _vehicle(client, ids)

    def fail(*args, **kwargs):
        raise checkout_service.OrderFromDraftError("catalog")

    monkeypatch.setattr(checkout_service, "create_order_from_draft", fail)
    response = client.post("/policyholder", data=valid_policyholder_data(), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/vehicle"
    conn = _conn(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM insurance_orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM insurance_order_files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0] == 0
        # still referenced by the draft -> recoverable by simply submitting again
        assert len(_draft(settings, client)[web_documents.DRAFT_KEY]) == 3
        assert web_documents.cleanup_storage(conn, settings) == 0  # too recent to be an orphan
        later = datetime.now(UTC) + timedelta(hours=settings.app.web_upload_orphan_hours + 1)
        assert web_documents.cleanup_storage(conn, settings, now=later) == 3  # abandoned -> removed
    finally:
        conn.close()
    assert not any(settings.app.uploads_dir.iterdir())


def test_cleanup_never_removes_documents_an_order_owns(settings, ids, ocr):
    _client, order = _web_order(settings, ids)
    conn = _conn(settings)
    try:
        far = datetime.now(UTC) + timedelta(days=3650)
        assert web_documents.cleanup_storage(conn, settings, now=far) == 0  # retention unset: kept
    finally:
        conn.close()
    assert all(web_documents.read_stored(settings, f.storage_path) for f in _files(settings, order.id))


def test_stored_names_can_never_escape_the_storage_directory(settings):
    for name in ("../web.db", "..", "/etc/passwd", "a/b.jpg", ".hidden"):
        assert web_documents.read_stored(settings, name) is None


# ------------------------------------------------ H: manual, isolation


def test_manual_website_order_gets_only_the_card(settings, ids, ocr, ge_bot):
    _client, order = _web_order(settings, ids, upload=False)
    assert _files(settings, order.id) == []
    jobs = _jobs(settings, order.id)
    assert sorted((j.kind, j.target_chat_id) for j in jobs) == sorted(
        [(bot_outbox.KIND_MANAGER_CARD, OWNER), (bot_outbox.KIND_MANAGER_CARD, MANAGER)]
    )
    sent, calls = _deliver(ge_bot)
    assert sent == 2 and not [c for c in calls if isinstance(c, SendDocument)]


def test_turkey_website_order_goes_to_the_turkey_bots_staff_only(settings, ids, ocr, ge_bot):
    tr_bot = BotHarness(settings, profile=_profile(settings, "osago24rt"), manager_ids={777}, owner_id=778)
    try:
        client = TestClient(app)
        _to_upload(client, country="TR")
        assert _upload(client).status_code == 303
        assert _vehicle(client, ids, model_year="2015").status_code == 303
        token = _submit(client, date_of_birth="1990-03-05")
        conn = _conn(settings)
        try:
            order = get_order_by_token(conn, token)
        finally:
            conn.close()
        jobs = _jobs(settings, order.id)
        assert {j.bot_key for j in jobs} == {"osago24rt"} and {j.target_chat_id for j in jobs} == {777, 778}
        assert _deliver(ge_bot) == (0, [])  # Georgia's bot never touches it
        sent, calls = _deliver(tr_bot)
        assert sent == 8 and len(sent_to(calls, 777, SendDocument)) == 3 and len(sent_to(calls, 778, SendDocument)) == 3
    finally:
        tr_bot.close()


def test_armenia_website_order_is_delivered_by_georgias_bot(settings):
    assert web_documents.delivery_bot_key(settings, "AM") == "osago24ge"
    assert web_documents.delivery_bot_key(settings, "TR") == "osago24rt"
    assert web_documents.delivery_bot_key(settings, "GE") == "osago24ge"
