"""Phase 3: /start -> category -> period -> start date -> data-entry-method
choice, driven through the real Dispatcher with an offline recording Bot
session (see tests/telegram_bot_helpers.py). Real config/config.yaml
pricing (the canonical business prices); a fresh SQLite file per test."""

import json
import logging
from datetime import timedelta

import pytest
from aiogram.methods import SendMessage

from app.checkout import service as checkout_service
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.dates.rules import GeorgiaDateRule, today_in_georgia
from app.formatting import format_rub
from app.sessions.repository import get_draft, merge_draft
from app.settings import load_settings
from app.telegram_bot import handlers as handlers_module
from app.telegram_bot.handlers import parse_start_date_input
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, screens, seed_catalog


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    loaded = load_settings(PROJECT_ROOT)
    seed_catalog(loaded.app.db_file)
    return loaded


@pytest.fixture
def harness(settings):
    h = BotHarness(settings)
    yield h
    h.close()


def _draft(settings, user_id, profile=TEST_PROFILE):
    conn = get_connection(settings.app.db_file)
    try:
        return get_draft(conn, session_id_for(profile.bot_key, user_id)) or {}
    finally:
        conn.close()


def _events(settings, user_id, name):
    conn = get_connection(settings.app.db_file)
    try:
        rows = conn.execute(
            "SELECT properties FROM insurance_analytics_events WHERE session_id = ? AND event_name = ? ORDER BY id",
            (session_id_for(TEST_PROFILE.bot_key, user_id), name),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["properties"]) if row["properties"] else None for row in rows]


def _order_count(settings):
    conn = get_connection(settings.app.db_file)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM insurance_orders").fetchone()["n"]
    finally:
        conn.close()


def _to_date_step(h, user, category="passenger_car", period="30d"):
    h.send_text(user, "/start")
    h.press(user, "m:apply")
    h.press(user, f"c:{category}")
    return h.press(user, f"p:{category}:{period}")


def _fmt(d):
    return d.strftime("%d.%m.%Y")


# ------------------------------------------------------------------- /start


def test_start_shows_intro_with_apply_button(harness, settings):
    user = make_user(1, "alice")
    screen = last_screen(harness.send_text(user, "/start"))
    assert screen.text == (
        "🇬🇪 ОСАГО Грузии\n\nОформите страховку автомобиля для поездки в Грузию онлайн.\n\nВыберите действие:"
    )
    assert buttons(screen) == [("🚗 Оформить страховку", "m:apply")]
    draft = _draft(settings, 1)
    assert draft["country_code"] == "GE"
    assert draft["channel"] == "telegram"
    assert draft["bot_key"] == TEST_PROFILE.bot_key
    assert (draft["telegram_user_id"], draft["telegram_chat_id"], draft["telegram_username"]) == (1, 1, "alice")
    assert draft.get("acquisition_source") is None
    assert _events(settings, 1, "bot_started") == [{"bot_key": "testbot", "source": None, "source_rejected": False}]


def test_start_with_valid_source_is_captured(harness, settings):
    harness.send_text(make_user(2), "/start upper_lars")
    assert _draft(settings, 2)["acquisition_source"] == "upper_lars"
    assert _events(settings, 2, "bot_started")[-1] == {"bot_key": "testbot", "source": "upper_lars", "source_rejected": False}


@pytest.mark.parametrize("payload", ["bad source!", "x" * 65, "привет", "a.b", "a/b"])
def test_invalid_source_is_rejected_and_never_stored(harness, settings, payload):
    screen = last_screen(harness.send_text(make_user(3), f"/start {payload}"))
    assert buttons(screen) == [("🚗 Оформить страховку", "m:apply")]  # still a normal start
    assert _draft(settings, 3).get("acquisition_source") is None
    event = _events(settings, 3, "bot_started")[-1]
    assert event == {"bot_key": "testbot", "source": None, "source_rejected": True}
    assert payload not in json.dumps(event, ensure_ascii=False)


def test_plain_start_never_erases_captured_source(harness, settings):
    user = make_user(4)
    harness.send_text(user, "/start telegram_group_1")
    harness.send_text(user, "/start")
    harness.send_text(user, "/start not valid!")
    assert _draft(settings, 4)["acquisition_source"] == "telegram_group_1"


def test_new_valid_source_replaces_old_one_last_touch(harness, settings):
    user = make_user(5)
    harness.send_text(user, "/start upper_lars")
    harness.send_text(user, "/start google")
    assert _draft(settings, 5)["acquisition_source"] == "google"


def test_start_does_not_wipe_draft_progress(harness, settings):
    user = make_user(6)
    _to_date_step(harness, user)
    harness.send_text(user, "/start")
    assert _draft(settings, 6)["period_code"] == "30d"


def test_group_chat_messages_are_ignored(harness):
    calls = harness.send_text(make_user(7), "/start", chat_type="group", chat_id=-100123)
    assert calls == []


# ----------------------------------------------------------------- category


def test_category_buttons_are_the_allowed_ge_categories(harness, settings):
    user = make_user(10)
    harness.send_text(user, "/start")
    screen = last_screen(harness.press(user, "m:apply"))
    assert screen.text == "Выберите тип транспортного средства:"
    conn = get_connection(settings.app.db_file)
    try:
        allowed = {c.code for c in checkout_service.list_offered_categories(conn, settings, "GE")}
    finally:
        conn.close()
    assert buttons(screen) == [
        ("🚗 Легковой автомобиль", "c:passenger_car"),
        ("🏍 Мотоцикл", "c:motorcycle"),
        ("🚛 Грузовой автомобиль", "c:truck"),
        ("🚌 Автобус", "c:bus"),
        ("🚚 Прицеп", "c:trailer"),
        ("🚜 Спецтехника", "c:special_vehicle"),
    ]
    assert {data.split(":", 1)[1] for _, data in buttons(screen)} == allowed


def test_unknown_or_stale_category_falls_back_to_category_list(harness):
    user = make_user(11)
    screen = last_screen(harness.press(user, "c:spaceship"))
    assert "недоступен" in screen.text
    assert ("🚗 Легковой автомобиль", "c:passenger_car") in buttons(screen)


# ------------------------------------------------------------------ periods


@pytest.mark.parametrize("category", ["passenger_car", "motorcycle", "truck", "bus", "trailer", "special_vehicle"])
def test_period_buttons_come_from_canonical_pricing(harness, settings, category):
    user = make_user(20)
    harness.send_text(user, "/start")
    screen = last_screen(harness.press(user, f"c:{category}"))
    periods = checkout_service.list_priced_periods(settings, "GE", category)
    assert periods, "canonical pricing must offer periods for every GE category"
    expected = [(f"{p.label} — {format_rub(p.price_rub)} ₽", f"p:{category}:{p.code}") for p in periods]
    assert buttons(screen)[:-1] == expected
    assert buttons(screen)[-1] == ("⬅️ Назад", "m:categories")
    for label, _ in expected:
        assert label in screen.text
    assert "Выберите срок страховки:" in screen.text


def test_passenger_car_period_screen_matches_business_prices(harness):
    user = make_user(21)
    screen = last_screen(harness.press(user, "c:passenger_car"))
    assert "15 дней — 1 349 ₽\n30 дней — 2 149 ₽\n90 дней — 3 649 ₽" in screen.text


def test_selecting_period_stores_canonical_price_and_asks_date(harness, settings):
    user = make_user(22)
    screen = last_screen(_to_date_step(harness, user, "motorcycle", "90d"))
    assert screen.text == "📅 Когда должна начать действовать страховка?"
    assert [text for text, _ in buttons(screen)] == ["Сегодня", "Завтра", "Ввести дату", "⬅️ Назад"]
    draft = _draft(settings, 22)
    assert (draft["vehicle_category_code"], draft["period_code"]) == ("motorcycle", "90d")
    assert draft["price_customer_minor"] == 289900


def test_tampered_period_payload_is_rejected(harness, settings):
    user = make_user(23)
    harness.send_text(user, "/start")
    calls = harness.press(user, "p:passenger_car:1y")
    assert "period_code" not in _draft(settings, 23)
    assert "Выберите срок страховки:" in last_screen(calls).text


# --------------------------------------------------------------- start date


def _summary_expected(start, *, label="🚗 Легковой автомобиль", period="30 дней", price="2 149"):
    return (
        f"Вы выбрали:\n\n{label}\n📅 Начало: {_fmt(start)}\n⏱ Срок: {period}\n💰 Стоимость: {price} ₽\n\n"
        "Как заполнить данные?"
    )


def test_today_button(harness, settings):
    user = make_user(30)
    _to_date_step(harness, user)
    screen = last_screen(harness.press(user, "d:today"))
    today = today_in_georgia()
    assert screen.text == _summary_expected(today)
    draft = _draft(settings, 30)
    assert draft["start_date"] == today.isoformat()
    assert draft["end_date"] == GeorgiaDateRule().compute_end_date(today, "30d").isoformat()


def test_tomorrow_button(harness, settings):
    user = make_user(31)
    _to_date_step(harness, user)
    screen = last_screen(harness.press(user, "d:tomorrow"))
    tomorrow = today_in_georgia() + timedelta(days=1)
    assert screen.text == _summary_expected(tomorrow)
    assert _draft(settings, 31)["start_date"] == tomorrow.isoformat()


def test_manual_valid_date(harness, settings):
    user = make_user(32)
    _to_date_step(harness, user)
    prompt = last_screen(harness.press(user, "d:manual"))
    assert "ДД.ММ.ГГГГ" in prompt.text
    start = today_in_georgia() + timedelta(days=12)
    screen = last_screen(harness.send_text(user, _fmt(start)))
    assert screen.text == _summary_expected(start)
    assert _draft(settings, 32)["start_date"] == start.isoformat()


def test_manual_past_date_rejected_and_can_retry(harness, settings):
    user = make_user(33)
    _to_date_step(harness, user)
    harness.press(user, "d:manual")
    yesterday = today_in_georgia() - timedelta(days=1)
    screen = last_screen(harness.send_text(user, _fmt(yesterday)))
    assert "Дата начала не может быть раньше сегодняшнего дня" in screen.text
    assert "start_date" not in _draft(settings, 33)
    later = today_in_georgia() + timedelta(days=3)
    assert last_screen(harness.send_text(user, _fmt(later))).text == _summary_expected(later)


@pytest.mark.parametrize("text", ["32.13.2026", "hello", "2026-10-02", "31.02.2027", "1.1.26"])
def test_manual_invalid_date_rejected(harness, settings, text):
    user = make_user(34)
    _to_date_step(harness, user)
    harness.press(user, "d:manual")
    screen = last_screen(harness.send_text(user, text))
    assert screen.text.startswith("Не удалось распознать дату.")
    assert "start_date" not in _draft(settings, 34)


def test_date_parser():
    assert parse_start_date_input("02.10.2026").isoformat() == "2026-10-02"
    assert parse_start_date_input(" 2.1.2027 ").isoformat() == "2027-01-02"
    assert parse_start_date_input("29.02.2027") is None
    assert parse_start_date_input("") is None


def test_changing_period_after_date_recomputes_end_date(harness, settings):
    user = make_user(35)
    _to_date_step(harness, user, period="15d")
    harness.press(user, "d:tomorrow")
    harness.press(user, "p:passenger_car:90d")
    draft = _draft(settings, 35)
    start = today_in_georgia() + timedelta(days=1)
    assert draft["end_date"] == (start + timedelta(days=90)).isoformat()


# ---------------------------------------------------- method-choice screen


def test_user_reaches_data_entry_method_screen(harness):
    user = make_user(40)
    _to_date_step(harness, user)
    screen = last_screen(harness.press(user, "d:tomorrow"))
    assert screen.text.endswith("Как заполнить данные?")
    assert buttons(screen) == [("📸 Загрузить документы", "e:documents"), ("✍️ Заполнить вручную", "e:manual")]


@pytest.mark.parametrize(
    ("choice", "expected_start", "event"),
    [("documents", "📸 Загрузите документы", "bot_document_upload_started"), ("manual", "🚗 Госномер автомобиля", "bot_manual_entry_started")],
)
def test_method_buttons_start_their_flow_without_creating_an_order(harness, settings, choice, expected_start, event):
    user = make_user(41)
    _to_date_step(harness, user)
    harness.press(user, "d:tomorrow")
    screen = last_screen(harness.press(user, f"e:{choice}"))
    assert screen.text.startswith(expected_start)
    assert _draft(settings, 41)["data_entry_method"] == choice
    assert _events(settings, 41, event) == [None]
    assert _order_count(settings) == 0


def test_method_button_with_expired_start_date_asks_for_date_again(harness, settings):
    user = make_user(42)
    _to_date_step(harness, user)
    harness.press(user, "d:tomorrow")
    conn = get_connection(settings.app.db_file)
    try:
        yesterday = today_in_georgia() - timedelta(days=1)
        merge_draft(conn, session_id_for(TEST_PROFILE.bot_key, 42), {"start_date": yesterday.isoformat()})
    finally:
        conn.close()
    screen = last_screen(harness.press(user, "e:manual"))
    assert screen.text == "📅 Когда должна начать действовать страховка?"


# ------------------------------------------------------ persistence/isolation


def test_draft_and_conversation_state_survive_restart(settings):
    user = make_user(50)
    first = BotHarness(settings)
    _to_date_step(first, user, "trailer", "15d")
    first.press(user, "d:manual")  # now waiting for a typed date
    first.close()

    restarted = BotHarness(settings)  # new Bot, Dispatcher, storage -- same DB
    try:
        start = today_in_georgia() + timedelta(days=4)
        screen = last_screen(restarted.send_text(user, _fmt(start)))
        assert screen.text == _summary_expected(start, label="🚚 Прицеп", period="15 дней", price="849")
    finally:
        restarted.close()


def test_old_buttons_still_work_after_restart(settings):
    user = make_user(51)
    first = BotHarness(settings)
    _to_date_step(first, user)
    first.close()
    restarted = BotHarness(settings)
    try:
        screen = last_screen(restarted.press(user, "d:tomorrow"))
        assert screen.text == _summary_expected(today_in_georgia() + timedelta(days=1))
    finally:
        restarted.close()


def test_two_users_never_share_a_draft(harness, settings):
    alice, bob = make_user(60, "alice"), make_user(61, "bob")
    harness.send_text(alice, "/start upper_lars")
    harness.send_text(bob, "/start google")
    harness.press(alice, "p:passenger_car:30d")
    harness.press(bob, "p:motorcycle:15d")
    harness.press(alice, "d:today")
    harness.press(bob, "d:manual")
    harness.send_text(bob, _fmt(today_in_georgia() + timedelta(days=7)))

    a, b = _draft(settings, 60), _draft(settings, 61)
    assert (a["vehicle_category_code"], a["period_code"], a["acquisition_source"]) == ("passenger_car", "30d", "upper_lars")
    assert (b["vehicle_category_code"], b["period_code"], b["acquisition_source"]) == ("motorcycle", "15d", "google")
    assert a["start_date"] == today_in_georgia().isoformat()
    assert b["start_date"] == (today_in_georgia() + timedelta(days=7)).isoformat()
    assert (a["telegram_username"], b["telegram_username"]) == ("alice", "bob")


def test_a_typed_date_from_another_user_does_not_leak(harness, settings):
    """Bob is waiting for a typed date; Alice (not waiting) types one --
    it must not be applied to either draft."""
    alice, bob = make_user(62), make_user(63)
    _to_date_step(harness, bob)
    harness.press(bob, "d:manual")
    _to_date_step(harness, alice)
    harness.send_text(alice, _fmt(today_in_georgia() + timedelta(days=2)))
    assert "start_date" not in _draft(settings, 62)
    assert "start_date" not in _draft(settings, 63)


# ------------------------------------------------------------------- safety


def test_free_text_outside_a_question_resumes_current_step(harness):
    user = make_user(70)
    _to_date_step(harness, user)
    screen = last_screen(harness.send_text(user, "привет"))
    assert screen.text.startswith("Пожалуйста, воспользуйтесь кнопками ниже.")
    assert "📅 Когда должна начать действовать страховка?" in screen.text


def test_unexpected_error_shows_generic_message_without_details(harness, monkeypatch, caplog):
    def _boom(*args, **kwargs):
        raise RuntimeError("internal detail AB123CD should never leak")

    monkeypatch.setattr(handlers_module, "record_start", _boom)
    with caplog.at_level(logging.ERROR):
        calls = harness.send_text(make_user(80), "/start")
    sent = [c for c in calls if isinstance(c, SendMessage)]
    assert [c.text for c in sent] == ["Что-то пошло не так. Попробуйте ещё раз или отправьте /start."]
    assert "AB123CD" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_only_expected_bot_api_methods_are_used(harness):
    """RecordingSession raises on anything else -- a full pass through the
    flow proves no unexpected (e.g. file/download/payment) API call."""
    user = make_user(90)
    _to_date_step(harness, user)
    harness.press(user, "d:tomorrow")
    calls = harness.press(user, "e:documents")
    assert screens(calls)
