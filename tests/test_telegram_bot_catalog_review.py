"""Live-test fixes: OCR-recognized vehicles absent from the catalog use the
catalog's own "Other" entries (never "not recognized"), the document text
stays visible, one consolidated review with single-field corrections, and
a manufacturer picker that searches the REAL catalog. Offline throughout."""

import io

import pytest
from aiogram.methods import SendMessage
from PIL import Image

from app.catalog.repository import get_other_manufacturer, get_other_model, upsert_manufacturer
from app.countries import COUNTRIES
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.ocr.models import OcrResult, VehicleDataCandidates
from app.ocr.parser import apply_other_fallback
from app.ocr.provider import OcrProvider
from app.orders.repository import list_telegram_orders
from app.sessions.repository import get_draft
from app.settings import load_settings
from app.telegram_bot import texts
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, nav, seed_catalog

PLATE, VIN = "B869BM155", "XZGFF07A6NA105975"
HAVAL = OcrResult(
    provider="fake", registration_number=PLATE, vin=VIN, chassis_number=None, manufacturer="HAVAL", model="H9 CC6490WM20B",
    policyholder_full_name="PETROV PETR", passport_number="751234567", citizenship="Russian Federation",
)
REVIEW = "✅ Документы распознаны"


class Provider(OcrProvider):
    def __init__(self, result):
        self.result, self.calls = result, 0

    def recognize(self, images):
        self.calls += 1
        return self.result


def _photo() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1000, 700), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


ALBUM = [("photo", name, _photo()) for name in ("tp-1", "tp-2", "passport")]


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


def _recognized(settings, user_id, result=HAVAL):
    h = BotHarness(settings, manager_ids={999}, ocr_provider=Provider(result))
    user = make_user(user_id, "alice")
    _to_documents(h, user)
    review = last_screen(h.send_album(user, f"grp-{user_id}", ALBUM))
    return h, user, review


# ----------------------------------------------------------- Other fallback


def test_brand_and_model_absent_from_catalog_use_other_and_keep_the_document_text(settings, ids):
    h, user, review = _recognized(settings, 1)
    try:
        assert review.text.startswith(REVIEW)
        for line in (f"Госномер: {PLATE}", f"VIN: {VIN}", "Марка: HAVAL", "Модель: H9 CC6490WM20B"):
            assert line in review.text
        assert "ℹ️ HAVAL / H9 CC6490WM20B отсутствует в каталоге.\nДля оформления будет использовано «Other»." in review.text
        assert "Не хватает" not in review.text and "не распознано" not in review.text and "Марка: Other" not in review.text
        draft = _draft(settings, 1)
        assert (draft["manufacturer_id"], draft["model_id"]) == (ids["Other"], ids["Other/Other"])  # real catalog rows
        assert (draft["vehicle_make_text"], draft["vehicle_model_text"]) == ("HAVAL", "H9 CC6490WM20B")
        assert last_screen(h.press(user, "vc")).text.startswith("✉️ Email")  # nothing about the car is asked again
    finally:
        h.close()


def test_known_brand_with_unknown_model_keeps_the_brand_and_uses_its_other_model(settings, ids):
    result = OcrResult(provider="fake", registration_number=PLATE, vin=VIN, chassis_number=None, manufacturer="Toyota", model="Crown Majesta")
    h, user, review = _recognized(settings, 2, result)
    try:
        assert "Марка: TOYOTA" in review.text and "Модель: Crown Majesta" in review.text
        assert "ℹ️ Crown Majesta отсутствует в каталоге." in review.text
        draft = _draft(settings, 2)
        assert (draft["manufacturer_id"], draft["model_id"]) == (ids["TOYOTA"], ids["TOYOTA/Other"])
        assert draft.get("vehicle_make_text") is None
    finally:
        h.close()


def test_known_brand_and_model_are_matched_as_before(settings, ids):
    result = OcrResult(provider="fake", registration_number=PLATE, vin=VIN, chassis_number=None, manufacturer="Toyota", model="Camry")
    h, user, review = _recognized(settings, 3, result)
    try:
        assert "Марка: TOYOTA" in review.text and "Модель: CAMRY" in review.text and "ℹ️" not in review.text
        assert (_draft(settings, 3)["manufacturer_id"], _draft(settings, 3)["model_id"]) == (ids["TOYOTA"], ids["TOYOTA/CAMRY"])
    finally:
        h.close()


def test_other_model_is_synced_on_demand_when_missing(settings, ids, monkeypatch):
    from app.catalog.repository import upsert_model
    from app.ocr import parser

    conn = get_connection(settings.app.db_file)
    try:
        lada = upsert_manufacturer(conn, external_id=77_001, name="LADA", is_popular=False)
        conn.commit()
        calls = []

        def fake_sync(connection, manufacturer):
            calls.append(manufacturer.name)
            upsert_model(connection, external_id=-1, manufacturer_id=manufacturer.id, name="Other")
            connection.commit()
            return True

        monkeypatch.setattr(parser, "sync_models_on_demand", fake_sync)
        candidates = VehicleDataCandidates(PLATE, "vin", VIN, lada, "LADA", None, "Niva Legend")
        updated, make_text, model_text = apply_other_fallback(conn, candidates)
        assert calls == ["LADA"]
        assert updated.model_id == get_other_model(conn, lada).id
        assert (make_text, model_text) == (None, "Niva Legend")
    finally:
        conn.close()


def test_nothing_read_stays_empty_not_other(settings, ids):
    conn = get_connection(settings.app.db_file)
    try:
        candidates = VehicleDataCandidates(PLATE, "vin", VIN, None, None, None, None)
        assert apply_other_fallback(conn, candidates) == (candidates, None, None)
        assert get_other_manufacturer(conn).id == ids["Other"]
    finally:
        conn.close()


def test_order_and_manager_card_keep_the_document_text(settings, ids):
    h, user, _ = _recognized(settings, 4)
    try:
        h.press(user, "vc")
        h.send_text(user, "petr@example.com")
        h.send_contact(user, "79001234567")
        h.press(user, "fc:continue")
        conn = get_connection(settings.app.db_file)
        try:
            (order,) = list_telegram_orders(conn, bot_key=TEST_PROFILE.bot_key, telegram_user_id=4)
        finally:
            conn.close()
        assert (order.vehicle_make, order.vehicle_model) == ("Other", "Other")  # catalog / TPL
        assert (order.vehicle_make_document, order.vehicle_model_document) == ("HAVAL", "H9 CC6490WM20B")
        calls = h.send_photo(user, _photo(), file_id="receipt")
        card = [c for c in calls if isinstance(c, SendMessage) and c.chat_id == 999][0]
        assert "Марка HAVAL (в каталоге: Other)" in card.text and "Модель H9 CC6490WM20B (в каталоге: Other)" in card.text
    finally:
        h.close()


# ------------------------------------------------------ consolidated review


def test_review_shows_vehicle_insurance_and_policyholder_with_compact_actions(settings, ids):
    h, user, review = _recognized(settings, 5)
    try:
        for line in ("🚗 Автомобиль", "📅 Страховка", "Категория: 🚗 Легковой автомобиль", "Период: 30 дней",
                     "Дата начала:", "Стоимость: 2 149 ₽", "👤 Страхователь", "ФИО: PETROV PETR",
                     "Паспорт: 751234567", "Гражданство: Россия", "Email: —", "Телефон: —"):
            assert line in review.text
        assert [text for text, _ in buttons(review)] == [
            "✅ Всё верно", "✏️ Госномер", "✏️ VIN", "✏️ Марка", "✏️ Модель", "✏️ ФИО", "✏️ Паспорт",
            "✏️ Гражданство", "✏️ Дата начала", "✏️ Период", "✏️ Email", "✏️ Телефон",  # a bot without fixed contacts
            "📷 Загрузить документы заново", "← Назад", "❌ Отменить оформление",
        ]
    finally:
        h.close()


def test_each_correction_returns_to_the_same_review(settings, ids):
    h, user, _ = _recognized(settings, 6)
    try:
        h.press(user, nav("plate", "checkout_review"))
        screen = last_screen(h.send_text(user, "NEW123"))
        assert screen.text.startswith(REVIEW) and "Госномер: NEW123" in screen.text

        h.press(user, nav("full_name", "checkout_review"))
        screen = last_screen(h.send_text(user, "Sidorov Semen"))
        assert screen.text.startswith(REVIEW) and "ФИО: SIDOROV SEMEN" in screen.text.upper()

        h.press(user, nav("periods", "checkout_review"))
        screen = last_screen(h.press(user, "p:passenger_car:90d"))
        assert screen.text.startswith(REVIEW) and "Период: 90 дней" in screen.text and "Стоимость: 3 649 ₽" in screen.text

        h.press(user, nav("date", "checkout_review"))
        screen = last_screen(h.press(user, "d:today"))
        assert screen.text.startswith(REVIEW)

        h.press(user, nav("manufacturer", "checkout_review"))
        h.press(user, f"mf:{ids['TOYOTA']}")
        screen = last_screen(h.press(user, f"md:{ids['TOYOTA/CAMRY']}"))
        assert screen.text.startswith(REVIEW) and "Марка: TOYOTA" in screen.text and "ℹ️" not in screen.text
        assert _draft(settings, 6).get("vehicle_make_text") is None
    finally:
        h.close()


# --------------------------------------------------------- manufacturer UI


def _to_manufacturer(settings, user_id):
    h = BotHarness(settings, manager_ids={999}, ocr_provider=None)
    user = make_user(user_id)
    h.send_text(user, "/start")
    h.press(user, "p:passenger_car:30d")
    h.press(user, "d:tomorrow")
    h.press(user, "e:manual")
    h.send_text(user, "AB123CD")
    return h, user, last_screen(h.send_text(user, VIN))


def test_popular_brands_are_labelled_shortcuts_with_search_and_other(settings, ids):
    h, user, screen = _to_manufacturer(settings, 20)
    try:
        assert "Введите название марки" in screen.text and "Популярные марки" in screen.text and "это не весь каталог" in screen.text
        data = dict((text, cb) for text, cb in buttons(screen))
        assert data["🔎 Найти другую марку"] == "n:manufacturer:"
        assert data["Other"] == f"mf:{ids['Other']}"
    finally:
        h.close()


def test_search_covers_the_whole_catalog_not_just_popular(settings, ids):
    h, user, _ = _to_manufacturer(settings, 21)
    try:
        screen = last_screen(h.send_text(user, "bmw"))  # BMW is not a "popular" row in the test catalog
        assert ("BMW", f"mf:{ids['BMW']}") in buttons(screen)
    finally:
        h.close()


def test_no_result_offers_other_and_keeps_the_typed_name(settings, ids):
    h, user, _ = _to_manufacturer(settings, 22)
    try:
        screen = last_screen(h.send_text(user, "HAVAL"))
        assert screen.text.startswith("🏭 Марка «HAVAL» не найдена в каталоге.\nМожно использовать «Other».")
        labels = [text for text, _ in buttons(screen)]
        assert "Other" in labels and "🔎 Искать другую марку" in labels and "← Назад" in labels
        model_screen = last_screen(h.press(user, f"mf:{ids['Other']}"))
        assert ("Other", f"md:{ids['Other/Other']}") in buttons(model_screen)
        review = last_screen(h.press(user, f"md:{ids['Other/Other']}"))
        assert "Марка: HAVAL" in review.text and "ℹ️ HAVAL отсутствует в каталоге." in review.text
        assert _draft(settings, 22)["vehicle_make_text"] == "HAVAL"
    finally:
        h.close()


# ------------------------------------------------------------------- copy


def test_no_out_of_five_counter_anywhere():
    """The document counter is always "N из 3"; the only {limit} left is the
    one-off "extra photos skipped" note, which is not a counter."""
    for name in dir(texts):
        value = getattr(texts, name)
        if not isinstance(value, str):
            continue
        assert "из 5" not in value, name
        if name != "DOCUMENTS_LIMIT_SKIPPED":
            assert "{limit}" not in value, name


def test_manual_entry_still_completes(settings, ids):
    h, user, _ = _to_manufacturer(settings, 23)
    try:
        h.press(user, f"mf:{ids['TOYOTA']}")
        review = last_screen(h.press(user, f"md:{ids['TOYOTA/CAMRY']}"))
        assert review.text.startswith("Проверьте данные автомобиля") and "Марка: TOYOTA ✅ из каталога" in review.text
        h.press(user, "vc")
        h.send_text(user, "Ivanov Ivan")
        h.send_text(user, "AB1234567")
        h.press(user, f"cz:{COUNTRIES.index('Russia')}")
        h.send_text(user, "ivan@example.com")
        review = last_screen(h.send_contact(user, "79001234567"))
        assert review.text.startswith("Проверьте данные:")  # the consolidated review, not "Проверьте заявку"
        assert buttons(review)[0] == ("✅ Всё верно", "fc:confirm")
    finally:
        h.close()
