"""Telegram albums (media groups) in the document step: one album -> one
OCR batch -> one progress message -> one review, however the items arrive.
Offline: RecordingOcrProvider, in-memory Telegram files, a tmp SQLite DB."""

import io
import json
import logging

import pytest
from aiogram.methods import EditMessageText, SendMessage
from PIL import Image, ImageDraw

from app.countries import COUNTRIES
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.ocr.models import OcrResult
from app.ocr.provider import OcrProvider, OcrProviderError
from app.orders import files as order_files
from app.orders.repository import list_telegram_orders
from app.sessions.repository import get_draft
from app.settings import load_settings
from app.telegram_bot import documents as documents_module
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, nav, seed_catalog

VIN = "WVWZZZ1JZXW000001"
PROGRESS = "⏳ Распознаю документы"


def _result(**fields) -> OcrResult:
    base = dict(provider="fake", registration_number=None, vin=None, chassis_number=None, manufacturer=None, model=None)
    base.update(fields)
    return OcrResult(**base)


FULL = _result(
    registration_number="AB123CD", vin=VIN, manufacturer="Toyota", model="Camry",
    policyholder_full_name="PETROV PETR", passport_number="751234567", citizenship="Russian Federation",
)


class RecordingOcrProvider(OcrProvider):
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[list] = []

    def recognize(self, images):
        self.calls.append(list(images))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def photo(size=(1000, 700)) -> bytes:
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle([0, 0, 60, 60], fill="red")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


ALBUM = [("photo", "tp-front", photo()), ("photo", "tp-back", photo()), ("photo", "passport", photo())]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", "+7 900 000-00-00")
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", "Тестов Т.")
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


def _harness(settings, provider):
    return BotHarness(settings, manager_ids={999}, ocr_provider=provider)


def _draft(settings, user_id):
    conn = get_connection(settings.app.db_file)
    try:
        return get_draft(conn, session_id_for(TEST_PROFILE.bot_key, user_id)) or {}
    finally:
        conn.close()


def _to_documents(h, user):
    h.send_text(user, "/start")
    h.press(user, "p:passenger_car:30d")
    h.press(user, "d:tomorrow")
    return h.press(user, "e:documents")


def _texts(calls, chat_id):
    return [c.text for c in calls if isinstance(c, (SendMessage, EditMessageText)) and c.chat_id == chat_id]


def _progress_messages(calls, chat_id):
    return [c for c in calls if isinstance(c, SendMessage) and c.chat_id == chat_id and PROGRESS in c.text]


def _reviews(calls, chat_id):
    """The consolidated review screens shown (vehicle + insurance + policyholder)."""
    return [t for t in _texts(calls, chat_id) if "Проверьте данные:" in t and "🚗 Автомобиль" in t]


# --------------------------------------------------------------- the screen


def test_upload_screen_has_no_recognize_button(settings, ids):
    h = _harness(settings, RecordingOcrProvider(FULL))
    try:
        screen = last_screen(_to_documents(h, make_user(1)))
        assert screen.text.startswith("📸 Загрузите документы")
        assert "обеих сторон техпаспорта" in screen.text and "загранпаспорта" in screen.text
        labels = [t for t, _ in buttons(screen)]
        assert not any("Распознать" in t for t in labels)
        assert "✍️ Ввести вручную" in labels
    finally:
        h.close()


# --------------------------------------------------------------------- A/B


@pytest.mark.parametrize("concurrent", [False, True])
def test_album_of_three_is_one_batch_one_progress_one_review(settings, ids, concurrent):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(2)
        _to_documents(h, user)
        calls = h.send_album(user, "grp-1", ALBUM, concurrent=concurrent)
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 3  # ONE call, all three photos
        assert len(_progress_messages(calls, 2)) == 1
        assert len(_reviews(calls, 2)) == 1
        # no per-item chatter: the only counter is inside the single progress message
        assert [t for t in _texts(calls, 2) if "Фото получено" in t and PROGRESS not in t] == []
        assert "Фото получено: 3 из 3" in _progress_messages(calls, 2)[0].text
        assert not any("из 5" in t for t in _texts(calls, 2))
        shown = [c for c in calls if isinstance(c, (SendMessage, EditMessageText))]
        assert not any("Все документы загружены" in text for c in shown for text, _ in buttons(c))
        assert not any("Что-то пошло не так" in t for t in _texts(calls, 2))
        draft = _draft(settings, 2)
        assert [f["file_unique_id"] for f in draft["document_files"]] == ["u-tp-front", "u-tp-back", "u-passport"]
        assert draft["pending_document_files"] == [] and draft["processed_media_groups"] == ["grp-1"]
    finally:
        h.close()


# ----------------------------------------------------------------------- C/D


def test_duplicate_items_in_one_album_are_stored_once(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(3)
        _to_documents(h, user)
        calls = h.send_album(user, "grp-dup", [ALBUM[0], ALBUM[0], ALBUM[1]])
        assert provider.calls == []  # 2 distinct photos: not the full set yet
        assert len(_draft(settings, 3)["pending_document_files"]) == 2
        assert any(t.startswith("Фото получено: 2 из 3") for t in _texts(calls, 3))
        h.send_photo(user, photo(), file_id="third")  # the missing photo completes the set
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 3
    finally:
        h.close()


def test_late_duplicate_after_processing_starts_nothing(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(4)
        _to_documents(h, user)
        h.send_album(user, "grp-late", ALBUM)
        h.press(user, nav("documents", "vehicle_review"))  # back in the document step
        calls = h.send_album(user, "grp-late", [ALBUM[2]])  # Telegram re-delivers an item
        assert len(provider.calls) == 1
        assert calls == []
    finally:
        h.close()


def test_late_new_item_of_a_processed_album_becomes_a_single_photo(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(5)
        _to_documents(h, user)
        h.send_album(user, "grp-x", ALBUM[:2])  # 2 of 3: waits for the rest
        assert provider.calls == []
        calls = h.send_album(user, "grp-x", [("photo", "straggler", photo())])
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 3  # now complete: ONE batch
        assert len(_reviews(calls, 5)) == 1
    finally:
        h.close()


# ------------------------------------------------------------------- E/F/G


def test_one_batch_fills_vehicle_and_passport_suggestions(settings, ids):
    h = _harness(settings, RecordingOcrProvider(FULL))
    try:
        user = make_user(6)
        _to_documents(h, user)
        calls = h.send_album(user, "grp-e", ALBUM)
        (review,) = _reviews(calls, 6)
        assert "Госномер: AB123CD" in review and f"VIN: {VIN}" in review
        assert "Марка: TOYOTA" in review and "Модель: CAMRY" in review
        assert "ФИО: PETROV PETR" in review and "Паспорт: 751234567" in review and "Гражданство: Россия" in review
        draft = _draft(settings, 6)
        assert (draft["full_name"], draft["identification_number"], draft["citizenship"]) == ("PETROV PETR", "751234567", "Russia")
        screen = last_screen(h.press(user, "vc"))
        assert screen.text.startswith("✉️ Email")  # only what is still missing is asked
    finally:
        h.close()


def test_two_albums_complement_each_other(settings, ids):
    provider = RecordingOcrProvider(
        _result(registration_number="AB123CD", vin=VIN, manufacturer="Toyota", model="Camry"),
        _result(policyholder_full_name="PETROV PETR", passport_number="751234567"),
    )
    h = _harness(settings, provider)
    try:
        user = make_user(7)
        _to_documents(h, user)
        h.send_album(user, "grp-vehicle", ALBUM)
        h.press(user, nav("documents", "checkout_review"))
        calls = h.send_album(user, "grp-passport", [("photo", f"p{i}", photo()) for i in range(3)])
        (review,) = _reviews(calls, 7)
        assert "Госномер: AB123CD" in review and "⚠️" not in review
        assert _draft(settings, 7)["ocr_policyholder_full_name"] == "PETROV PETR"
    finally:
        h.close()


def test_typed_value_is_kept_and_a_conflict_is_shown(settings, ids):
    provider = RecordingOcrProvider(_result(registration_number="OCR999", vin=VIN, manufacturer="BMW", model="X5"))
    h = _harness(settings, provider)
    try:
        user = make_user(8)
        h.send_text(user, "/start")
        h.press(user, "p:passenger_car:30d")
        h.press(user, "d:tomorrow")
        h.press(user, "e:manual")
        h.send_text(user, "TYPED1")
        h.send_text(user, "XTA21099043000001")
        h.press(user, f"mf:{ids['TOYOTA']}")
        h.press(user, nav("documents"))
        calls = h.send_album(user, "grp-f", ALBUM)
        (review,) = _reviews(calls, 8)
        assert "Госномер: TYPED1" in review and "VIN: XTA21099043000001" in review
        assert "Марка: TOYOTA" in review  # never switched to the OCR'd BMW
        assert "⚠️ На новых фото распознано иначе: госномер, VIN или номер шасси, марка" in review
        draft = _draft(settings, 8)
        assert (draft["registration_number"], draft["identifier"], draft["manufacturer_id"]) == ("TYPED1", "XTA21099043000001", ids["TOYOTA"])
    finally:
        h.close()


# ----------------------------------------------------------------------- H/I


def test_one_unusable_image_does_not_sink_the_album(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(9)
        _to_documents(h, user)
        calls = h.send_album(user, "grp-h", [("photo", "broken", b"not an image"), ALBUM[1], ALBUM[2]])
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 2
        (review,) = _reviews(calls, 9)
        assert "Фото 1 не удалось обработать" in review
    finally:
        h.close()


def test_pdf_in_an_album_is_never_sent_to_ocr(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(10)
        _to_documents(h, user)
        calls = h.send_album(user, "grp-pdf", [*ALBUM, ("document", "scan-pdf", b"%PDF-1.4", "application/pdf")])
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 3
        (review,) = _reviews(calls, 10)
        assert review.count("не подходит для распознавания") == 1
    finally:
        h.close()


@pytest.mark.parametrize(
    "error",
    [OcrProviderError("boom", classification={"category": "timeout"}), ValueError("unexpected provider bug")],
    ids=["provider_error", "unexpected_error"],
)
def test_whole_batch_failure_gives_one_message_and_can_be_retried(settings, ids, error, caplog):
    provider = RecordingOcrProvider(error, FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(11)
        _to_documents(h, user)
        with caplog.at_level(logging.INFO):
            calls = h.send_album(user, "grp-fail", ALBUM)
        texts = _texts(calls, 11)
        assert len(_progress_messages(calls, 11)) == 1
        assert sum("Не удалось распознать документы" in t for t in texts) == 1
        assert not any("Что-то пошло не так" in t for t in texts)
        assert any(t.startswith("Распознавание не удалось") for t in texts)  # the one progress message is closed, not left spinning
        assert len(_draft(settings, 11)["pending_document_files"]) == 3  # nothing lost
        failure = last_screen(calls)
        labels = [t for t, _ in buttons(failure)]
        assert labels[:2] == ["🔄 Попробовать распознать ещё раз", "📷 Загрузить другие фото"]
        assert not any("Все документы загружены" in t for t in labels)
        assert "failed at ocr" in caplog.text and type(error).__name__ in caplog.text
        for secret in ("tp-front", "passport", VIN, "unexpected provider bug"):
            assert secret not in caplog.text

        retry = h.press(user, buttons(failure)[0][1])  # the explicit retry
        assert len(provider.calls) == 2 and len(_reviews(retry, 11)) == 1
    finally:
        h.close()


def test_catalog_outage_keeps_the_recognized_text(settings, ids, monkeypatch, caplog):
    def broken(conn, result):
        raise ValueError("tpl.ge returned HTML")

    monkeypatch.setattr(documents_module, "build_candidates", broken)
    h = _harness(settings, RecordingOcrProvider(FULL))
    try:
        user = make_user(12)
        _to_documents(h, user)
        with caplog.at_level(logging.WARNING):
            calls = h.send_album(user, "grp-cat", ALBUM)
        (review,) = _reviews(calls, 12)
        assert "Госномер: AB123CD" in review and f"VIN: {VIN}" in review
        assert "Марка: Toyota" in review and "Модель: Camry" in review
        assert "ℹ️ Toyota / Camry отсутствует в каталоге." in review
        assert "failed at catalog_match" in caplog.text
    finally:
        h.close()


def test_catalog_sync_bad_json_is_a_sync_failure_not_an_exception(monkeypatch):
    import httpx

    from app.catalog import sync as sync_module
    from app.catalog.models import Manufacturer

    def html_page(client, external_id):
        raise ValueError("Expecting value: line 1 column 1")  # what response.json() raises on HTML

    monkeypatch.setattr(sync_module.tpl_client, "fetch_models", html_page)
    monkeypatch.setattr(sync_module.tpl_client, "new_client", lambda timeout: httpx.Client())
    manufacturer = Manufacturer(id=1, external_id=1, name="X", is_popular=False, active=True, models_synced_at=None)
    assert sync_module.sync_models_on_demand(None, manufacturer) is False


# ----------------------------------------------------------------------- J


def test_single_photos_count_to_three_then_recognize_automatically(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(13)
        start = last_screen(_to_documents(h, user))
        assert "Фото получено: 0 из 3" in start.text
        first = last_screen(h.send_photo(user, photo(), file_id="one"))
        second = last_screen(h.send_photo(user, photo(), file_id="two"))
        assert first.text.startswith("Фото получено: 1 из 3") and second.text.startswith("Фото получено: 2 из 3")
        for screen in (start, first, second):
            assert not any("Все документы загружены" in t or "Распознать" in t for t, _ in buttons(screen))
        assert provider.calls == []
        calls = h.send_photo(user, photo(), file_id="three")  # the third photo starts recognition by itself
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 3
        (progress,) = _progress_messages(calls, 13)
        assert "Фото получено: 3 из 3" in progress.text
        assert len(_reviews(calls, 13)) == 1
    finally:
        h.close()


def test_old_recognize_buttons_still_work(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(14)
        _to_documents(h, user)
        h.send_photo(user, photo(), file_id="one")
        assert _reviews(h.press(user, "dc:recognize"), 14)
    finally:
        h.close()


# ----------------------------------------------------------------------- K


def test_restart_mid_album_then_resend(settings, ids):
    provider = RecordingOcrProvider(FULL)
    user = make_user(15)
    first = _harness(settings, provider)
    _to_documents(first, user)
    first.send_album(user, "grp-old", ALBUM[:2], flush=False)  # bot stops before the album completes
    first.close()
    assert len(_draft(settings, 15)["pending_document_files"]) == 2  # nothing lost
    assert provider.calls == []

    restarted = _harness(settings, provider)
    try:
        # the customer simply resends the album (Telegram gives new file ids)
        new_album = [("photo", "new-front", photo()), ("photo", "new-back", photo()), ("photo", "new-passport", photo())]
        calls = restarted.send_album(user, "grp-new", new_album)
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 3  # the stale half-album was replaced, not mixed in
        assert len(_reviews(calls, 15)) == 1
    finally:
        restarted.close()


def test_restart_mid_album_then_done_button(settings, ids):
    provider = RecordingOcrProvider(FULL)
    user = make_user(16)
    first = _harness(settings, provider)
    _to_documents(first, user)
    first.send_album(user, "grp-r", ALBUM, flush=False)
    files = dict(first.session.files)
    first.close()
    restarted = _harness(settings, provider)
    restarted.session.files.update(files)  # Telegram still has the uploaded files
    try:
        screen = last_screen(restarted.send_text(user, "я вернулся"))  # any message resumes the current step
        assert screen.text == "Фото получено: 3 из 3"
        assert ("🔍 Распознать загруженные фото", "dc:done") in buttons(screen)
        assert _reviews(restarted.press(user, "dc:done"), 16)
        assert len(provider.calls) == 1
    finally:
        restarted.close()


def test_more_than_five_photos_in_an_album(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(17)
        _to_documents(h, user)
        calls = h.send_album(user, "grp-big", [("photo", f"p{i}", photo()) for i in range(7)])
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 5
        (review,) = _reviews(calls, 17)
        assert review.count("лишние фото пропущены") == 1
    finally:
        h.close()


def test_album_after_leaving_the_document_step_is_not_processed(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(18)
        _to_documents(h, user)
        h.send_album(user, "grp-left", ALBUM, flush=False)
        h.press(user, "e:manual")  # customer switched to manual entry before the album settled
        calls = h.flush_albums()
        assert provider.calls == [] and _progress_messages(calls, 18) == []
    finally:
        h.close()


# --------------------------------------------------------------------- L/M


def _place_order(h, user, ids):
    h.send_text(user, "/start")
    h.press(user, "p:passenger_car:30d")
    h.press(user, "d:tomorrow")
    h.press(user, "e:manual")
    h.send_text(user, "AB123CD")
    h.send_text(user, VIN)
    h.press(user, f"mf:{ids['TOYOTA']}")
    h.press(user, f"md:{ids['TOYOTA/CAMRY']}")
    h.press(user, "vc")
    h.send_text(user, "Ivanov Ivan")
    h.send_text(user, "AB1234567")
    h.press(user, f"cz:{COUNTRIES.index('Russia')}")
    h.send_text(user, "ivan@example.com")
    h.send_contact(user, "79001234567")
    h.press(user, "fc:continue")
    conn = get_connection(h.dispatcher["settings"].app.db_file)
    try:
        (order,) = list_telegram_orders(conn, bot_key=TEST_PROFILE.bot_key, telegram_user_id=user.id)
    finally:
        conn.close()
    return order


def test_checkout_album_never_becomes_a_payment_receipt(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(19)
        _to_documents(h, user)
        h.send_album(user, "grp-docs", ALBUM)
        conn = get_connection(settings.app.db_file)
        try:
            receipts = conn.execute("SELECT COUNT(*) FROM insurance_order_files WHERE kind = 'payment_receipt'").fetchone()[0]
            orders = conn.execute("SELECT COUNT(*) FROM insurance_orders").fetchone()[0]
        finally:
            conn.close()
        assert (receipts, orders) == (0, 0)
    finally:
        h.close()


def test_payment_receipt_album_never_goes_to_ocr(settings, ids):
    provider = RecordingOcrProvider(FULL)
    h = _harness(settings, provider)
    try:
        user = make_user(20)
        order = _place_order(h, user, ids)
        h.send_album(user, "grp-receipt", [("photo", "receipt-1", photo()), ("photo", "receipt-2", photo())])
        assert provider.calls == []
        conn = get_connection(settings.app.db_file)
        try:
            receipts = order_files.list_files(conn, order.id, order_files.KIND_PAYMENT_RECEIPT)
        finally:
            conn.close()
        assert [r.telegram_file_id for r in receipts] == ["receipt-1", "receipt-2"]
        assert "pending_document_files" not in _draft(settings, 20)
    finally:
        h.close()


def test_ocr_logs_and_events_contain_no_personal_data(settings, ids, caplog):
    h = _harness(settings, RecordingOcrProvider(FULL))
    try:
        user = make_user(21)
        _to_documents(h, user)
        with caplog.at_level(logging.DEBUG):
            h.send_album(user, "grp-privacy", ALBUM)
        conn = get_connection(settings.app.db_file)
        try:
            events = json.dumps([dict(r) for r in conn.execute("SELECT event_name, properties FROM insurance_analytics_events")], ensure_ascii=False)
        finally:
            conn.close()
        for secret in ("tp-front", "u-passport", VIN, "AB123CD", "PETROV", "751234567", "grp-privacy"):
            assert secret not in caplog.text
            assert secret not in events
        assert "OCR batch" in caplog.text and "files=3" in caplog.text
    finally:
        h.close()
