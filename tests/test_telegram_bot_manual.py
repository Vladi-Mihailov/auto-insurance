"""Phase 4: manual vehicle entry, vehicle review, policyholder, final review
-- driven through the real Dispatcher with the offline harness. Real
config.yaml pricing; a fresh SQLite file (seeded catalog) per test."""

import json
from datetime import timedelta

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.countries import COUNTRIES
from app.db import get_connection
from app.dates.rules import today_in_georgia
from app.deps import PROJECT_ROOT
from app.sessions.repository import get_draft, merge_draft
from app.settings import load_settings
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, nav, seed_catalog

VIN = "WVWZZZ1JZXW000001"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


@pytest.fixture
def harness(settings, ids):
    h = BotHarness(settings)
    yield h
    h.close()


def _draft(settings, user_id):
    conn = get_connection(settings.app.db_file)
    try:
        return get_draft(conn, session_id_for(TEST_PROFILE.bot_key, user_id)) or {}
    finally:
        conn.close()


def _merge(settings, user_id, updates):
    conn = get_connection(settings.app.db_file)
    try:
        merge_draft(conn, session_id_for(TEST_PROFILE.bot_key, user_id), updates)
    finally:
        conn.close()


def _events(settings, user_id):
    conn = get_connection(settings.app.db_file)
    try:
        rows = conn.execute(
            "SELECT event_name, properties FROM insurance_analytics_events WHERE session_id = ? ORDER BY id",
            (session_id_for(TEST_PROFILE.bot_key, user_id),),
        ).fetchall()
    finally:
        conn.close()
    return [(row["event_name"], json.loads(row["properties"]) if row["properties"] else None) for row in rows]


def _event_names(settings, user_id):
    return [name for name, _ in _events(settings, user_id)]


def _order_count(settings):
    conn = get_connection(settings.app.db_file)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM insurance_orders").fetchone()["n"]
    finally:
        conn.close()


def _to_method(h, user, period="30d"):
    h.send_text(user, "/start")
    h.press(user, "m:apply")
    h.press(user, f"p:passenger_car:{period}")
    return h.press(user, "d:tomorrow")


def _vehicle_via_vin(h, user, ids, *, plate="AB123CD"):
    _to_method(h, user)
    h.press(user, "e:manual")
    h.send_text(user, plate)
    h.send_text(user, VIN)
    h.press(user, f"mf:{ids['TOYOTA']}")
    return h.press(user, f"md:{ids['TOYOTA/CAMRY']}")


def _policyholder(h, user):
    h.send_text(user, "Ivanov Ivan")
    h.send_text(user, "AB1234567")
    h.press(user, f"cz:{COUNTRIES.index('Russia')}")
    h.send_text(user, "ivan@example.com")
    return h.send_contact(user, "79001234567")


def _complete_flow(h, user, ids):
    _vehicle_via_vin(h, user, ids)
    h.press(user, "vc")
    return _policyholder(h, user)


# ------------------------------------------------------------ happy paths


def test_manual_complete_flow_with_vin(harness, settings, ids):
    user = make_user(1)
    screen = last_screen(harness.press(user, "e:manual") if _to_method(harness, user) else None)
    assert screen.text.startswith("🚗 Госномер автомобиля")

    screen = last_screen(harness.send_text(user, "ab 123 cd"))
    assert screen.text.startswith("🔢 VIN")
    screen = last_screen(harness.send_text(user, VIN.lower()))
    assert screen.text.startswith("🏭 Марка автомобиля")
    assert ("TOYOTA", f"mf:{ids['TOYOTA']}") in buttons(screen)  # popular manufacturers offered
    screen = last_screen(harness.press(user, f"mf:{ids['TOYOTA']}"))
    assert screen.text.startswith("🚘 Модель TOYOTA")
    assert [text for text, _ in buttons(screen)][:4] == ["CAMRY", "COROLLA", "LAND CRUISER", "Other"]
    review = last_screen(harness.press(user, f"md:{ids['TOYOTA/CAMRY']}"))
    assert review.text == (
        "Проверьте данные автомобиля\n\n"
        "Госномер: AB 123 CD\n"
        f"VIN: {VIN}\n"
        "Шасси: не требуется (указан VIN)\n"
        "Марка: TOYOTA ✅ из каталога\n"
        "Модель: CAMRY ✅ из каталога"
    )
    assert [text for text, _ in buttons(review)] == [
        "✅ Всё верно",
        "✏️ Изменить госномер",
        "✏️ Изменить VIN",
        "✏️ Изменить шасси",
        "✏️ Изменить марку",
        "✏️ Изменить модель",
        "📷 Загрузить ещё фото",
        "← Назад",
        "❌ Отменить оформление",
    ]

    screen = last_screen(harness.press(user, "vc"))
    assert screen.text.startswith("👤 ФИО страхователя")
    calls = _policyholder(harness, user)
    sent = [c for c in calls if isinstance(c, SendMessage)]
    assert sent[0].text == "✅ Телефон сохранён." and isinstance(sent[0].reply_markup, ReplyKeyboardRemove)
    final = last_screen(calls)
    tomorrow = today_in_georgia() + timedelta(days=1)
    # The end of manual entry is the SAME consolidated review as after OCR --
    # its "✅ Всё верно" is the final confirmation (no "Проверьте заявку").
    assert final.text == (
        "Проверьте данные:\n\n"
        "🚗 Автомобиль\nГосномер: AB 123 CD\nVIN: " + VIN + "\nШасси: —\nМарка: TOYOTA\nМодель: CAMRY\n\n"
        "📅 Страховка\nКатегория: 🚗 Легковой автомобиль\nПериод: 30 дней\n"
        f"Дата начала: {tomorrow:%d.%m.%Y}\nОкончание: {tomorrow + timedelta(days=30):%d.%m.%Y}\nСтоимость: 2 149 ₽\n\n"
        "👤 Страхователь\nФИО: Ivanov Ivan\nПаспорт: AB1234567\nГражданство: Россия\n"
        "Email: ivan@example.com\nТелефон: +79001234567"
    )
    assert [text for text, _ in buttons(final)][0] == "✅ Всё верно"
    assert "✅ Продолжить" not in [text for text, _ in buttons(final)]

    draft = _draft(settings, 1)
    assert (draft["registration_number"], draft["identifier_type"], draft["identifier"]) == ("AB 123 CD", "vin", VIN)
    assert (draft["manufacturer_id"], draft["model_id"]) == (ids["TOYOTA"], ids["TOYOTA/CAMRY"])
    assert (draft["full_name"], draft["identification_number"], draft["citizenship"]) == ("Ivanov Ivan", "AB1234567", "Russia")
    assert (draft["contact_email"], draft["contact_phone"]) == ("ivan@example.com", "+79001234567")
    assert draft["vehicle_confirmed"] is True and draft["data_entry_method"] == "manual"

    assert _order_count(settings) == 0  # no order until "✅ Всё верно"
    after = last_screen(harness.press(user, "fc:confirm"))
    # Payment details aren't configured in this test -> fail-safe message
    # (the full payment flow is covered in tests/test_telegram_bot_orders.py).
    assert "Оплата временно недоступна" in after.text
    assert _order_count(settings) == 1

    names = _event_names(settings, 1)
    for expected in ("bot_manual_entry_started", "bot_vehicle_data_confirmed", "bot_policyholder_completed", "bot_checkout_review_shown"):
        assert expected in names


def test_chassis_without_vin_path(harness, settings, ids):
    user = make_user(2)
    _to_method(harness, user)
    harness.press(user, "e:manual")
    harness.send_text(user, "XY777ZZ")
    screen = last_screen(harness.press(user, nav("chassis")))
    assert screen.text.startswith("🔢 Номер шасси")
    screen = last_screen(harness.send_text(user, "fr12345678"))
    assert screen.text.startswith("🏭 Марка автомобиля")
    assert ("← Назад", nav("chassis")) in buttons(screen)  # back returns to the chassis step
    harness.press(user, f"mf:{ids['BMW']}")
    review = last_screen(harness.press(user, f"md:{ids['BMW/318']}"))
    assert "VIN: не указан (указан номер шасси)" in review.text
    assert "Шасси: FR12345678" in review.text
    assert last_screen(harness.press(user, "vc")).text.startswith("👤 ФИО страхователя")
    draft = _draft(settings, 2)
    assert (draft["identifier_type"], draft["identifier"]) == ("chassis", "FR12345678")


def test_switching_from_chassis_back_to_vin_replaces_the_identifier(harness, settings, ids):
    user = make_user(3)
    _to_method(harness, user)
    harness.press(user, "e:manual")
    harness.send_text(user, "XY777ZZ")
    harness.press(user, nav("chassis"))
    harness.send_text(user, "FR12345678")
    harness.press(user, nav("vin", "vehicle_review"))
    harness.send_text(user, VIN)
    draft = _draft(settings, 3)
    assert (draft["identifier_type"], draft["identifier"]) == ("vin", VIN)


# ------------------------------------------------------------- validation


@pytest.mark.parametrize("plate", ["!", "AB@123", "A" * 16])
def test_invalid_plate_rejected_and_step_repeated(harness, settings, ids, plate):
    user = make_user(10)
    _to_method(harness, user)
    harness.press(user, "e:manual")
    screen = last_screen(harness.send_text(user, plate))
    assert "Регистрационный номер" in screen.text and "Попробуйте ещё раз" in screen.text
    assert screen.text.endswith("например AB123CD.")
    assert "registration_number" not in _draft(settings, 10)
    assert last_screen(harness.send_text(user, "AB123CD")).text.startswith("🔢 VIN")


@pytest.mark.parametrize("vin", ["AB", "WVW-ZZZ!", "X" * 26])
def test_invalid_vin_rejected(harness, settings, ids, vin):
    user = make_user(11)
    _to_method(harness, user)
    harness.press(user, "e:manual")
    harness.send_text(user, "AB123CD")
    screen = last_screen(harness.send_text(user, vin))
    assert screen.text.startswith("VIN:")
    assert "identifier" not in _draft(settings, 11)


def test_manufacturer_search_and_model_filter(harness, settings, ids):
    user = make_user(12)
    _to_method(harness, user)
    harness.press(user, "e:manual")
    harness.send_text(user, "AB123CD")
    harness.send_text(user, VIN)
    screen = last_screen(harness.send_text(user, "volks"))
    assert "Найдено в каталоге по запросу «volks»" in screen.text
    assert ("VOLKSWAGEN", f"mf:{ids['VOLKSWAGEN']}") in buttons(screen)
    assert all(text != "TOYOTA" for text, _ in buttons(screen))
    screen = last_screen(harness.send_text(user, "zzzz"))
    assert screen.text.startswith("🏭 Марка «zzzz» не найдена в каталоге.\nМожно использовать «Other».")
    assert ("Other", f"mf:{ids['Other']}") in buttons(screen)
    assert ("🔎 Искать другую марку", "n:manufacturer:") in buttons(screen)

    harness.press(user, f"mf:{ids['VOLKSWAGEN']}")
    screen = last_screen(harness.send_text(user, "gol"))
    assert [text for text, _ in buttons(screen) if not text.startswith(("⬅️", "✖️", "❌"))] == ["GOLF", "Other"]
    harness.press(user, f"md:{ids['VOLKSWAGEN/Other']}")  # the catalog's own "Other" is a valid model
    assert _draft(settings, 12)["model_id"] == ids["VOLKSWAGEN/Other"]


def test_model_of_another_manufacturer_is_rejected(harness, settings, ids):
    user = make_user(13)
    _to_method(harness, user)
    harness.press(user, "e:manual")
    harness.send_text(user, "AB123CD")
    harness.send_text(user, VIN)
    harness.press(user, f"mf:{ids['TOYOTA']}")
    screen = last_screen(harness.press(user, f"md:{ids['BMW/X5']}"))  # tampered/stale payload
    assert screen.text.startswith("🚘 Модель TOYOTA")
    assert _draft(settings, 13).get("model_id") is None


def test_unknown_manufacturer_id_is_rejected(harness, settings, ids):
    user = make_user(14)
    _to_method(harness, user)
    harness.press(user, "e:manual")
    screen = last_screen(harness.press(user, "mf:999999"))
    assert screen.text.startswith("🏭 Марка автомобиля")
    assert _draft(settings, 14).get("manufacturer_id") is None


def test_models_synced_on_demand_off_the_event_loop(harness, settings, ids, monkeypatch):
    from app.catalog.repository import upsert_manufacturer, upsert_model
    from app.telegram_bot import steps

    conn = get_connection(settings.app.db_file)
    try:
        never_synced = upsert_manufacturer(conn, external_id=59_999, name="LADA", is_popular=False)
        conn.commit()
    finally:
        conn.close()
    calls = []

    def fake_sync(conn, manufacturer):
        calls.append(manufacturer.name)
        upsert_model(conn, external_id=1, manufacturer_id=manufacturer.id, name="NIVA")
        conn.execute("UPDATE insurance_manufacturers SET models_synced_at = 'now' WHERE id = ?", (manufacturer.id,))
        conn.commit()
        return True

    monkeypatch.setattr(steps, "sync_models_on_demand", fake_sync)
    user = make_user(15)
    _to_method(harness, user)
    harness.press(user, "e:manual")
    screen = last_screen(harness.press(user, f"mf:{never_synced}"))
    assert calls == ["LADA"]
    assert "NIVA" in [text for text, _ in buttons(screen)]


# ----------------------------------------------------- review / confirm


def test_confirmation_blocked_when_vehicle_incomplete(harness, settings, ids):
    user = make_user(20)
    _to_method(harness, user)
    harness.press(user, "e:manual")
    harness.send_text(user, "AB123CD")
    review = last_screen(harness.press(user, nav("vehicle_review")))
    assert "Госномер: AB123CD" in review.text
    assert "VIN: не указано" in review.text and "Марка: не указано" in review.text
    assert "Не хватает данных: VIN или номер шасси, марка, модель" in review.text
    calls = harness.press(user, "vc")
    alert = [c for c in calls if isinstance(c, AnswerCallbackQuery)][0]
    assert alert.show_alert and "Не хватает данных" in alert.text
    assert _draft(settings, 20).get("vehicle_confirmed") is not True


def test_policyholder_steps_unreachable_before_vehicle_confirmed(harness, settings, ids):
    user = make_user(21)
    _vehicle_via_vin(harness, user, ids)
    screen = last_screen(harness.press(user, nav("full_name")))
    assert screen.text.startswith("Проверьте данные автомобиля")


def test_edit_field_from_review_returns_to_review_and_needs_reconfirmation(harness, settings, ids):
    user = make_user(22)
    _vehicle_via_vin(harness, user, ids)
    harness.press(user, "vc")
    harness.press(user, nav("plate", "vehicle_review"))
    review = last_screen(harness.send_text(user, "NEW555"))
    assert review.text.startswith("Проверьте данные автомобиля")
    assert "Госномер: NEW555" in review.text
    assert _draft(settings, 22)["vehicle_confirmed"] is False


def test_changing_manufacturer_from_review_requires_a_new_model(harness, settings, ids):
    user = make_user(23)
    _vehicle_via_vin(harness, user, ids)
    harness.press(user, nav("manufacturer", "vehicle_review"))
    screen = last_screen(harness.press(user, f"mf:{ids['BMW']}"))
    assert screen.text.startswith("🚘 Модель BMW")
    assert _draft(settings, 23)["model_id"] is None
    review = last_screen(harness.press(user, f"md:{ids['BMW/X5']}"))
    assert "Марка: BMW ✅" in review.text and "Модель: X5 ✅" in review.text


def test_keep_button_accepts_existing_value(harness, settings, ids):
    user = make_user(24)
    _vehicle_via_vin(harness, user, ids)
    harness.press(user, nav("plate"))
    screen = last_screen(harness.press(user, "k:plate"))
    assert screen.text.startswith("🔢 VIN")
    assert _draft(settings, 24)["registration_number"] == "AB123CD"


def test_back_navigation(harness, settings, ids):
    user = make_user(25)
    _to_method(harness, user)
    plate = last_screen(harness.press(user, "e:manual"))
    assert ("← Назад", nav("method")) in buttons(plate)
    assert last_screen(harness.press(user, nav("method"))).text.startswith("Вы выбрали:")
    harness.press(user, "e:manual")
    harness.send_text(user, "AB123CD")
    assert last_screen(harness.press(user, nav("plate"))).text.startswith("🚗 Госномер автомобиля")


# ------------------------------------------------------------ policyholder


def _to_policyholder(h, user, ids):
    _vehicle_via_vin(h, user, ids)
    h.press(user, "vc")


@pytest.mark.parametrize("name", ["Иванов Иван", "I", "Ivanov1"])
def test_full_name_must_be_latin(harness, settings, ids, name):
    user = make_user(30)
    _to_policyholder(harness, user, ids)
    screen = last_screen(harness.send_text(user, name))
    assert screen.text.startswith("ФИО:")
    assert "full_name" not in _draft(settings, 30)


def test_invalid_email_rejected(harness, settings, ids):
    user = make_user(31)
    _to_policyholder(harness, user, ids)
    harness.send_text(user, "Ivanov Ivan")
    harness.send_text(user, "AB1234567")
    harness.press(user, f"cz:{COUNTRIES.index('Russia')}")
    screen = last_screen(harness.send_text(user, "not-an-email"))
    assert screen.text.startswith("Email: укажите корректный адрес")


@pytest.mark.parametrize(("text", "expected"), [("Россия", "Russia"), ("Kazakhstan", "Kazakhstan"), ("russian federation", "Russia")])
def test_citizenship_typed_in_russian_or_english(harness, settings, ids, text, expected):
    user = make_user(32)
    _to_policyholder(harness, user, ids)
    harness.send_text(user, "Ivanov Ivan")
    harness.send_text(user, "AB1234567")
    screen = last_screen(harness.send_text(user, text))
    assert screen.text.startswith("✉️ Email")
    assert _draft(settings, 32)["citizenship"] == expected


def test_unknown_citizenship_rejected(harness, settings, ids):
    user = make_user(33)
    _to_policyholder(harness, user, ids)
    harness.send_text(user, "Ivanov Ivan")
    harness.send_text(user, "AB1234567")
    screen = last_screen(harness.send_text(user, "Narnia"))
    assert screen.text.startswith("Не удалось определить страну")
    assert "citizenship" not in _draft(settings, 33)


def test_phone_is_required_for_telegram(harness, settings, ids):
    user = make_user(34)
    _to_policyholder(harness, user, ids)
    harness.send_text(user, "Ivanov Ivan")
    harness.send_text(user, "AB1234567")
    harness.press(user, f"cz:{COUNTRIES.index('Russia')}")
    phone_prompt = last_screen(harness.send_text(user, "ivan@example.com"))
    assert phone_prompt.text.startswith("📱 Телефон")
    assert isinstance(phone_prompt.reply_markup, ReplyKeyboardMarkup)
    assert phone_prompt.reply_markup.keyboard[0][0].request_contact is True

    # no order without a phone: "✅ Всё верно" (and an old "✅ Продолжить") asks for it
    review = last_screen(harness.press(user, nav("final_review")))  # an old button: the consolidated review
    assert review.text.startswith("Проверьте данные:") and "Телефон: —" in review.text
    assert last_screen(harness.press(user, "fc:confirm")).text.startswith("📱 Телефон")
    assert last_screen(harness.press(user, "fc:continue")).text.startswith("📱 Телефон")
    assert _order_count(settings) == 0
    assert last_screen(harness.send_text(user, "call me")).text.startswith("Телефон:")
    assert "contact_phone" not in _draft(settings, 34)
    assert last_screen(harness.send_text(user, "+995 555 12 34 56")).text.startswith("Проверьте данные:")


def test_someone_elses_shared_contact_is_rejected(harness, settings, ids):
    user = make_user(35)
    _to_policyholder(harness, user, ids)
    harness.send_text(user, "Ivanov Ivan")
    harness.send_text(user, "AB1234567")
    harness.press(user, f"cz:{COUNTRIES.index('Russia')}")
    harness.send_text(user, "ivan@example.com")
    screen = last_screen(harness.send_contact(user, "79990000000", contact_user_id=777))
    assert "свой собственный номер" in screen.text
    assert "contact_phone" not in _draft(settings, 35)


def test_ocr_suggestion_offered_and_usable(harness, settings, ids):
    user = make_user(36)
    _vehicle_via_vin(harness, user, ids)
    _merge(settings, 36, {"ocr_policyholder_full_name": "PETROV PETR", "ocr_identification_number": "75 1234567"})
    screen = last_screen(harness.press(user, "vc"))
    assert ("✅ Из документа: PETROV PETR", "sg:full_name") in buttons(screen)
    screen = last_screen(harness.press(user, "sg:full_name"))
    assert screen.text.startswith("🛂 Номер паспорта")
    assert ("✅ Из документа: 75 1234567", "sg:passport") in buttons(screen)
    assert _draft(settings, 36)["full_name"] == "PETROV PETR"


# ------------------------------------------------------------ final review


def test_review_edits_return_to_the_review(harness, settings, ids):
    user = make_user(40)
    review = last_screen(_complete_flow(harness, user, ids))
    # a bot without fixed contacts: email/phone are editable right on the review
    assert ("✏️ Email", nav("email", "checkout_review")) in buttons(review)
    assert ("✏️ Телефон", nav("phone", "checkout_review")) in buttons(review)
    harness.press(user, nav("contact_email", "checkout_review"))  # not a real step name -> ignored safely
    harness.press(user, nav("email", "checkout_review"))
    final = last_screen(harness.send_text(user, "new@example.com"))
    assert final.text.startswith("Проверьте данные:") and "Email: new@example.com" in final.text
    # buttons of the former "Проверьте заявку" screen (older messages) land on the review
    assert last_screen(harness.press(user, nav("policyholder_menu"))).text.startswith("Проверьте данные:")
    harness.press(user, nav("email", "final_review"))
    assert last_screen(harness.send_text(user, "newer@example.com")).text.startswith("Проверьте данные:")

    vehicle = last_screen(harness.press(user, nav("vehicle_review", "final_review")))
    assert vehicle.text.startswith("Проверьте данные автомобиля")
    assert last_screen(harness.press(user, "vc")).text.startswith("Проверьте данные:")
    assert _order_count(settings) == 0  # editing never creates an order


def test_changing_insurance_returns_to_the_review_with_new_price(harness, settings, ids):
    user = make_user(41)
    _complete_flow(harness, user, ids)
    harness.press(user, nav("categories"))
    harness.press(user, "p:passenger_car:90d")
    final = last_screen(harness.press(user, "d:today"))
    assert final.text.startswith("Проверьте данные:")
    assert "Период: 90 дней" in final.text and "Стоимость: 3 649 ₽" in final.text


# ---------------------------------------------------- restart / resume


def test_progress_survives_restart_mid_entry(settings, ids):
    user = make_user(50)
    first = BotHarness(settings)
    _to_method(first, user)
    first.press(user, "e:manual")
    first.send_text(user, "AB123CD")  # now waiting for the VIN
    first.close()
    restarted = BotHarness(settings)
    try:
        screen = last_screen(restarted.send_text(user, VIN))
        assert screen.text.startswith("🏭 Марка автомобиля")
        assert _draft(settings, 50)["identifier"] == VIN
    finally:
        restarted.close()


def test_edit_return_target_survives_restart(settings, ids):
    user = make_user(51)
    first = BotHarness(settings)
    _vehicle_via_vin(first, user, ids)
    first.press(user, nav("plate", "vehicle_review"))
    first.close()
    restarted = BotHarness(settings)
    try:
        assert last_screen(restarted.send_text(user, "ZZ999ZZ")).text.startswith("Проверьте данные автомобиля")
    finally:
        restarted.close()


def test_random_message_resumes_the_right_step(harness, settings, ids):
    user = make_user(52)
    _vehicle_via_vin(harness, user, ids)
    harness.press(user, "vc")
    harness.send_text(user, "Ivanov Ivan")
    screen = last_screen(harness.press(user, nav("resume")))
    assert screen.text.startswith("🛂 Номер паспорта")


def test_start_over_keeps_only_identity_and_source(harness, settings, ids):
    user = make_user(53, "alice")
    harness.send_text(user, "/start upper_lars")
    _complete_flow(harness, user, ids)
    confirm = last_screen(harness.press(user, nav("restart")))
    assert confirm.text.startswith("❌ Отменить текущее оформление?")
    cancelled = last_screen(harness.press(user, nav("restart_confirmed")))
    assert cancelled.text.startswith("Оформление отменено.")
    assert [t for t, _ in buttons(cancelled)] == ["🚗 Оформить страховку"]
    draft = _draft(settings, 53)
    assert draft["acquisition_source"] == "upper_lars" and draft["telegram_username"] == "alice"
    for key in ("vehicle_category_code", "registration_number", "identifier", "full_name", "checkout_id"):
        assert key not in draft
    assert last_screen(harness.press(user, "m:apply")).text == "Выберите тип транспортного средства:"


def test_start_over_can_be_cancelled(harness, settings, ids):
    user = make_user(54)
    _vehicle_via_vin(harness, user, ids)
    harness.press(user, nav("restart"))
    assert last_screen(harness.press(user, nav("resume"))).text.startswith("Проверьте данные автомобиля")
    assert _draft(settings, 54)["registration_number"] == "AB123CD"


# ---------------------------------------------------------------- privacy


def test_analytics_never_contain_personal_data(harness, settings, ids):
    user = make_user(60)
    _complete_flow(harness, user, ids)
    harness.press(user, "fc:continue")
    dumped = json.dumps(_events(settings, 60), ensure_ascii=False)
    for secret in (VIN, "AB123CD", "AB1234567", "Ivanov", "ivan@example.com", "79001234567"):
        assert secret not in dumped


def test_two_customers_stay_separate(harness, settings, ids):
    alice, bob = make_user(61), make_user(62)
    _vehicle_via_vin(harness, alice, ids, plate="ALICE1")
    _to_method(harness, bob)
    harness.press(bob, "e:manual")
    harness.send_text(bob, "BOB2")
    assert _draft(settings, 61)["registration_number"] == "ALICE1"
    assert _draft(settings, 62)["registration_number"] == "BOB2"
    assert "identifier" not in _draft(settings, 62)
