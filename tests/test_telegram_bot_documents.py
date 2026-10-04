"""Phase 4: document photo upload + OCR in the bot. Never a real OpenAI
call: RecordingOcrProvider returns canned OcrResults and records exactly
which images each call carried. Never a real Telegram download: the harness
serves registered in-memory bytes."""

import io
import json

import pytest
from aiogram.methods import AnswerCallbackQuery
from PIL import Image, ImageDraw

from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.ocr.models import OcrResult
from app.ocr.orientation import OrientationDetector, OrientationGuess
from app.ocr.provider import OcrProvider, OcrProviderError
from app.sessions.repository import get_draft
from app.settings import load_settings
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, nav, seed_catalog

VIN = "WVWZZZ1JZXW000001"
REVIEW = "✅ Документы распознаны"


def _result(**fields) -> OcrResult:
    base = dict(provider="fake", registration_number=None, vin=None, chassis_number=None, manufacturer=None, model=None)
    base.update(fields)
    return OcrResult(**base)


COMPLETE = _result(registration_number="AB123CD", vin=VIN, manufacturer="Toyota", model="Camry")


class RecordingOcrProvider(OcrProvider):
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[list[tuple[bytes, str]]] = []

    def recognize(self, images):
        self.calls.append(list(images))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FixedDetector(OrientationDetector):
    name = "fixed"

    def __init__(self, guess):
        self.guess = guess

    def detect(self, image):
        return self.guess


def photo_bytes(size=(1000, 700)) -> bytes:
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle([0, 0, 60, 60], fill="red")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


def _harness(settings, provider, detector=None):
    return BotHarness(settings, ocr_provider=provider, orientation_detector=detector)


def _draft(settings, user_id):
    conn = get_connection(settings.app.db_file)
    try:
        return get_draft(conn, session_id_for(TEST_PROFILE.bot_key, user_id)) or {}
    finally:
        conn.close()


def _events(settings, user_id, name=None):
    conn = get_connection(settings.app.db_file)
    try:
        rows = conn.execute(
            "SELECT event_name, properties FROM insurance_analytics_events WHERE session_id = ? ORDER BY id",
            (session_id_for(TEST_PROFILE.bot_key, user_id),),
        ).fetchall()
    finally:
        conn.close()
    events = [(r["event_name"], json.loads(r["properties"]) if r["properties"] else None) for r in rows]
    return [props for event, props in events if event == name] if name else events


def _to_documents(h, user):
    h.send_text(user, "/start")
    h.press(user, "p:passenger_car:30d")
    h.press(user, "d:tomorrow")
    return h.press(user, "e:documents")


def _received_sizes(call):
    return [Image.open(io.BytesIO(data)).size for data, _ in call]


# ----------------------------------------------------------------- happy


def test_ocr_success_shows_review_and_never_auto_confirms(settings, ids):
    provider = RecordingOcrProvider(COMPLETE)
    h = _harness(settings, provider)
    try:
        user = make_user(1)
        prompt = last_screen(_to_documents(h, user))
        assert prompt.text.startswith("📸 Загрузите документы")
        reply = last_screen(h.send_photo(user, photo_bytes(), file_id="f1"))
        assert reply.text.startswith("Фото получено: 1 из 3")
        assert not any("Все документы загружены" in t for t, _ in buttons(reply))

        calls = h.press(user, "dc:recognize")
        review = last_screen(calls)
        assert any("⏳ Распознаю документы" in (getattr(c, "text", None) or "") for c in calls)
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 1  # one call for the batch
        assert review.text.startswith("✅ Документы распознаны\n\nПроверьте данные:\n\n🚗 Автомобиль\n")
        for line in ("Госномер: AB123CD", f"VIN: {VIN}", "Шасси: —", "Марка: TOYOTA", "Модель: CAMRY",
                     "📅 Страховка", "Период: 30 дней", "Стоимость: 2 149 ₽", "👤 Страхователь", "ФИО: —"):
            assert line in review.text
        draft = _draft(settings, 1)
        assert draft.get("vehicle_confirmed") is not True  # the customer still has to confirm
        assert (draft["manufacturer_id"], draft["model_id"]) == (ids["TOYOTA"], ids["TOYOTA/CAMRY"])
        assert draft["pending_document_files"] == []
        assert [f["file_unique_id"] for f in draft["document_files"]] == ["u-f1"]
        assert h.session.downloads == ["f1"]
        (completed,) = _events(settings, 1, "bot_ocr_completed")
        assert completed["complete"] is True and completed["retry_used"] is False and completed["files_count"] == 1
        assert last_screen(h.press(user, "vc")).text.startswith("👤 ФИО страхователя")
    finally:
        h.close()


def test_several_photos_go_into_one_call(settings, ids):
    provider = RecordingOcrProvider(COMPLETE)
    h = _harness(settings, provider)
    try:
        user = make_user(2)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="front")
        h.send_photo(user, photo_bytes(), file_id="back")
        h.send_document(user, photo_bytes(), file_id="scan", mime_type="image/jpeg")
        h.press(user, "dc:recognize")
        assert len(provider.calls) == 1 and len(provider.calls[0]) == 3
    finally:
        h.close()


def test_passport_fields_become_policyholder_suggestions(settings, ids):
    provider = RecordingOcrProvider(
        _result(**COMPLETE.__dict__ | {"policyholder_full_name": "PETROV PETR", "passport_number": "75 1234567", "citizenship": "Russian Federation"})
    )
    h = _harness(settings, provider)
    try:
        user = make_user(3)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="f")
        review = last_screen(h.press(user, "dc:recognize"))
        assert "ФИО: PETROV PETR" in review.text and "Паспорт: 75 1234567" in review.text
        draft = _draft(settings, 3)
        assert (draft["full_name"], draft["identification_number"], draft["citizenship"]) == ("PETROV PETR", "75 1234567", "Russia")
        assert last_screen(h.press(user, "vc")).text.startswith("✉️ Email")
    finally:
        h.close()


# ------------------------------------------------------ incomplete/failed


def test_incomplete_ocr_can_be_completed_by_hand(settings, ids):
    provider = RecordingOcrProvider(_result(registration_number="AB123CD", manufacturer="Toyota Motor Corporation", model="Kamri"))
    h = _harness(settings, provider, FixedDetector(OrientationGuess(rotate_clockwise=0, confidence=4.0)))
    try:
        user = make_user(10)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="f")
        review = last_screen(h.press(user, "dc:recognize"))
        assert "VIN: —" in review.text and "Шасси: —" in review.text
        assert "Марка: TOYOTA" in review.text  # "... Motor Corporation" prefix match
        assert "Модель: Kamri" in review.text and "ℹ️ Kamri отсутствует в каталоге." in review.text
        assert "Не хватает: VIN или номер шасси" in review.text
        assert _draft(settings, 10)["model_id"] == ids["TOYOTA/Other"]

        alert = [c for c in h.press(user, "vc") if isinstance(c, AnswerCallbackQuery)][0]
        assert alert.show_alert and "Не хватает данных" in alert.text

        h.press(user, nav("vin", "checkout_review"))
        review = last_screen(h.send_text(user, VIN))
        assert review.text.startswith(REVIEW) and f"VIN: {VIN}" in review.text  # back on the SAME review
        model_step = last_screen(h.press(user, nav("model", "checkout_review")))
        assert "Распознано в документе: «Kamri»" in model_step.text
        review = last_screen(h.press(user, f"md:{ids['TOYOTA/CAMRY']}"))
        assert review.text.startswith(REVIEW) and "Модель: CAMRY" in review.text and "ℹ️" not in review.text
        assert last_screen(h.press(user, "vc")).text.startswith("👤 ФИО страхователя")
        (completed,) = _events(settings, 10, "bot_ocr_completed")
        assert completed["complete"] is False
    finally:
        h.close()


def test_provider_failure_keeps_photos_for_a_retry(settings, ids):
    provider = RecordingOcrProvider(OcrProviderError("boom", classification={"category": "timeout"}), COMPLETE)
    h = _harness(settings, provider)
    try:
        user = make_user(11)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="f")
        screen = last_screen(h.press(user, "dc:recognize"))
        assert screen.text.startswith("Не удалось распознать документы.")
        assert len(_draft(settings, 11)["pending_document_files"]) == 1
        (failed,) = _events(settings, 11, "bot_ocr_failed")
        assert failed["reason"] == "provider_error" and failed["category"] == "timeout"
        # an old "Распознать"/"done" button no longer re-runs a failed batch ...
        assert last_screen(h.press(user, "dc:recognize")).text.startswith("📸 Загрузите документы")
        assert len(provider.calls) == 1
        # ... only the explicit retry does
        assert last_screen(h.press(user, "dr:1")).text.startswith(REVIEW)
    finally:
        h.close()


def test_nothing_recognized_asks_for_another_photo(settings, ids):
    h = _harness(settings, RecordingOcrProvider(_result()))
    try:
        user = make_user(12)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="f")
        screen = last_screen(h.press(user, "dc:recognize"))
        assert "не удалось найти данные документа" in screen.text
        assert _draft(settings, 12)["pending_document_files"] == []
        assert _events(settings, 12, "bot_ocr_failed")[0]["reason"] == "nothing_found"
    finally:
        h.close()


def test_ocr_unavailable_without_provider(settings, ids):
    h = _harness(settings, None)
    try:
        user = make_user(13)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="f")
        screen = last_screen(h.press(user, "dc:recognize"))
        assert screen.text.startswith("Распознавание документов сейчас недоступно")
        assert ("✍️ Ввести вручную", "e:manual") in buttons(screen)
    finally:
        h.close()


def test_invalid_image_bytes_are_skipped(settings, ids):
    provider = RecordingOcrProvider(COMPLETE)
    h = _harness(settings, provider)
    try:
        user = make_user(14)
        _to_documents(h, user)
        h.send_photo(user, b"not an image at all", file_id="bad")
        h.send_photo(user, photo_bytes(), file_id="good")
        review = last_screen(h.press(user, "dc:recognize"))
        assert "Фото 1 не удалось обработать" in review.text
        assert len(provider.calls[0]) == 1
    finally:
        h.close()


def test_only_invalid_images_never_reach_the_provider(settings, ids):
    provider = RecordingOcrProvider(COMPLETE)
    h = _harness(settings, provider)
    try:
        user = make_user(15)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes((200, 150)), file_id="tiny")
        screen = last_screen(h.press(user, "dc:recognize"))
        assert "слишком маленькое" in screen.text
        assert provider.calls == []
        assert _events(settings, 15, "bot_ocr_failed")[0]["reason"] == "no_valid_images"
    finally:
        h.close()


# ---------------------------------------------------------------- uploads


def test_upload_limits(settings, ids):
    h = _harness(settings, RecordingOcrProvider(COMPLETE))
    try:
        user = make_user(20)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="same")
        assert "уже добавлено" in last_screen(h.send_photo(user, photo_bytes(), file_id="same")).text
        assert "не подходит" in last_screen(h.send_document(user, b"%PDF-1.4", file_id="pdf", mime_type="application/pdf")).text
        assert len(_draft(settings, 20)["pending_document_files"]) == 1
        h.send_photo(user, photo_bytes(), file_id="second")
        assert last_screen(h.send_photo(user, photo_bytes(), file_id="third")).text.startswith(REVIEW)  # 3 -> automatic
    finally:
        h.close()


def test_oversized_file_rejected_before_download(settings, ids):
    h = _harness(settings, RecordingOcrProvider(COMPLETE))
    try:
        user = make_user(21)
        _to_documents(h, user)
        screen = last_screen(h.send_photo(user, photo_bytes(), file_id="big", file_size=25 * 1024 * 1024))
        assert "слишком большой" in screen.text
        assert "pending_document_files" not in _draft(settings, 21) or _draft(settings, 21)["pending_document_files"] == []
        assert h.session.downloads == []
    finally:
        h.close()


def test_done_without_photos_runs_nothing(settings, ids):
    provider = RecordingOcrProvider(COMPLETE)
    h = _harness(settings, provider)
    try:
        user = make_user(22)
        _to_documents(h, user)
        screen = last_screen(h.press(user, "dc:done"))
        assert provider.calls == []
        assert screen.text.startswith("📸 Загрузите документы")
    finally:
        h.close()


# ------------------------------------------------------------ orientation


def test_rotated_photo_is_sent_upright(settings, ids):
    provider = RecordingOcrProvider(COMPLETE)
    h = _harness(settings, provider, FixedDetector(OrientationGuess(rotate_clockwise=90, confidence=4.2)))
    try:
        user = make_user(30)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes((700, 1000)), file_id="sideways")
        h.press(user, "dc:recognize")
        assert _received_sizes(provider.calls[0]) == [(1000, 700)]
        assert _events(settings, 30, "bot_ocr_completed")[0]["rotated_images"] == 1
    finally:
        h.close()


def test_one_orientation_retry_when_incomplete_and_orientation_unknown(settings, ids):
    first = _result(registration_number="AB123CD", manufacturer="Toyota")
    second = _result(registration_number="ZZ000ZZ", vin=VIN, manufacturer="BMW", model="Camry")
    provider = RecordingOcrProvider(first, second)
    h = _harness(settings, provider, None)  # no detector -> orientation unknown
    try:
        user = make_user(31)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="f")
        review = last_screen(h.press(user, "dc:recognize"))
        assert len(provider.calls) == 2  # never more than one extra call
        assert _received_sizes(provider.calls[1]) == [(1000, 700), (700, 1000), (1000, 700), (700, 1000)]
        # the retry only filled gaps: first-attempt values win
        assert "Госномер: AB123CD" in review.text and f"VIN: {VIN}" in review.text
        assert "Марка: TOYOTA" in review.text and "Модель: CAMRY" in review.text
        assert _events(settings, 31, "bot_ocr_completed")[0]["retry_used"] is True
    finally:
        h.close()


def test_no_retry_when_orientation_was_confirmed(settings, ids):
    provider = RecordingOcrProvider(_result(registration_number="AB123CD"))
    h = _harness(settings, provider, FixedDetector(OrientationGuess(rotate_clockwise=0, confidence=4.0)))
    try:
        user = make_user(32)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="f")
        h.press(user, "dc:recognize")
        assert len(provider.calls) == 1
    finally:
        h.close()


def test_no_retry_when_first_result_is_complete(settings, ids):
    provider = RecordingOcrProvider(COMPLETE)
    h = _harness(settings, provider, None)
    try:
        user = make_user(33)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="f")
        h.press(user, "dc:recognize")
        assert len(provider.calls) == 1
    finally:
        h.close()


# ------------------------------------------------------------ merging


def test_more_photos_enrich_the_same_draft_without_overwriting(settings, ids):
    provider = RecordingOcrProvider(
        _result(registration_number="AB123CD", manufacturer="Toyota"),
        _result(registration_number="XX999XX", vin=VIN, manufacturer="Toyota", model="Camry"),
    )
    detector = FixedDetector(OrientationGuess(rotate_clockwise=0, confidence=4.0))
    h = _harness(settings, provider, detector)
    try:
        user = make_user(40)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="front")
        h.press(user, "dc:recognize")
        more = last_screen(h.press(user, nav("documents", "checkout_review")))
        assert more.text.startswith("📸 Загрузите документы")
        h.send_photo(user, photo_bytes(), file_id="back")
        review = last_screen(h.press(user, "dc:recognize"))
        assert "Госномер: AB123CD" in review.text  # the earlier value is kept ...
        assert "⚠️ На новых фото распознано иначе: госномер" in review.text  # ... and the difference surfaced
        assert f"VIN: {VIN}" in review.text and "Модель: CAMRY" in review.text and "ℹ️" not in review.text  # gaps filled
        draft = _draft(settings, 40)
        assert [f["file_unique_id"] for f in draft["document_files"]] == ["u-front", "u-back"]
    finally:
        h.close()


def test_ocr_never_overwrites_a_typed_value(settings, ids):
    provider = RecordingOcrProvider(_result(registration_number="OCR111", vin=VIN, manufacturer="Toyota", model="Camry"))
    h = _harness(settings, provider, FixedDetector(OrientationGuess(rotate_clockwise=0, confidence=4.0)))
    try:
        user = make_user(41)
        h.send_text(user, "/start")
        h.press(user, "p:passenger_car:30d")
        h.press(user, "d:tomorrow")
        h.press(user, "e:manual")
        h.send_text(user, "TYPED1")
        h.press(user, nav("documents"))
        h.send_photo(user, photo_bytes(), file_id="f")
        review = last_screen(h.press(user, "dc:recognize"))
        assert "Госномер: TYPED1" in review.text
        assert _draft(settings, 41)["registration_number"] == "TYPED1"
    finally:
        h.close()


# ----------------------------------------------------- restart / privacy


def test_pending_photos_survive_restart(settings, ids):
    user = make_user(50)
    provider = RecordingOcrProvider(COMPLETE)
    first = _harness(settings, provider)
    _to_documents(first, user)
    first.send_photo(user, photo_bytes(), file_id="f")
    files = dict(first.session.files)
    first.close()
    restarted = _harness(settings, provider)
    try:
        restarted.session.files.update(files)  # Telegram still has the file
        assert last_screen(restarted.press(user, "dc:recognize")).text.startswith(REVIEW)
    finally:
        restarted.close()


def test_ocr_analytics_contain_no_document_values(settings, ids):
    provider = RecordingOcrProvider(
        _result(**COMPLETE.__dict__ | {"policyholder_full_name": "PETROV PETR", "passport_number": "751234567"})
    )
    h = _harness(settings, provider)
    try:
        user = make_user(51)
        _to_documents(h, user)
        h.send_photo(user, photo_bytes(), file_id="secret-file-id")
        h.press(user, "dc:recognize")
        dumped = json.dumps(_events(settings, 51), ensure_ascii=False)
        for value in (VIN, "AB123CD", "PETROV", "751234567", "secret-file-id", "Toyota", "Camry"):
            assert value not in dumped
    finally:
        h.close()
