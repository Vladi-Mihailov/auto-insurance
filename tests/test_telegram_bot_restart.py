"""Cancel / fresh start / global /start.

- /start works from every checkout state and never force-resumes: it offers
  "🚗 Новая страховка" next to "↩️ Продолжить оформление" (checkout in
  progress) or "↩️ Вернуться к заказу" (an unfinished order);
- "❌ Отменить оформление" asks for confirmation and then really resets;
- a reset keeps only identity, acquisition source and the profile's fixed
  contacts -- and never touches insurance_orders;
- the next order gets a NEW client_checkout_id;
- buttons of the abandoned checkout, and a late OCR/album result of it, can
  never write into the fresh one.

All offline: fake Bot API session, fake OCR provider, a tmp DB."""

import dataclasses
import threading

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText

from app.countries import COUNTRIES
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.ocr.models import OcrResult
from app.ocr.provider import OcrProvider
from app.orders.repository import list_telegram_orders
from app.sessions.repository import get_draft, merge_draft
from app.settings import load_settings
from app.telegram_bot import texts
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, nav, screens, seed_catalog

VIN = "WVWZZZ1JZXW000001"
MANAGER = 999
FIXED = dataclasses.replace(TEST_PROFILE, customer_email="tplgee@mail.ru", customer_phone="+995 574 22 06 25")
CATEGORIES = "Выберите тип транспортного средства:"
COMPLETE = OcrResult(provider="fake", registration_number="AB123CD", vin=VIN, chassis_number=None, manufacturer="Toyota", model="Camry")


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


@pytest.fixture
def harness(settings, ids):
    h = BotHarness(settings, manager_ids={MANAGER})
    yield h
    h.close()


class BlockingOcrProvider(OcrProvider):
    """Holds recognition until the test releases it -- a batch that is
    still running while the customer resets the checkout."""

    def __init__(self, result=COMPLETE):
        self.result = result
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = 0

    def recognize(self, images):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=10), "test never released the OCR call"
        return self.result


class InstantOcrProvider(OcrProvider):
    def recognize(self, images):
        return COMPLETE


# ------------------------------------------------------------------ helpers


def _conn(settings):
    return get_connection(settings.app.db_file)


def _draft(settings, user_id, profile=TEST_PROFILE):
    conn = _conn(settings)
    try:
        return get_draft(conn, session_id_for(profile.bot_key, user_id)) or {}
    finally:
        conn.close()


def _merge(settings, user_id, updates):
    conn = _conn(settings)
    try:
        merge_draft(conn, session_id_for(TEST_PROFILE.bot_key, user_id), updates)
    finally:
        conn.close()


def _orders(settings, user_id):
    conn = _conn(settings)
    try:
        return list_telegram_orders(conn, bot_key=TEST_PROFILE.bot_key, telegram_user_id=user_id)
    finally:
        conn.close()


def _order_rows(settings):
    conn = _conn(settings)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM insurance_orders ORDER BY id").fetchall()]
    finally:
        conn.close()


def _events(settings, name):
    conn = _conn(settings)
    try:
        return conn.execute("SELECT COUNT(*) FROM insurance_analytics_events WHERE event_name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


def _to_method(h, user, start="/start"):
    h.send_text(user, start)
    h.press(user, "p:passenger_car:30d")
    return h.press(user, "d:tomorrow")


def _to_vehicle_review(h, user, ids):
    _to_method(h, user)
    h.press(user, "e:manual")
    h.send_text(user, "AB123CD")
    h.send_text(user, VIN)
    h.press(user, f"mf:{ids['TOYOTA']}")
    return h.press(user, f"md:{ids['TOYOTA/CAMRY']}")


def _to_final_review(h, user, ids, *, contacts=True):
    _to_vehicle_review(h, user, ids)
    h.press(user, "vc")
    h.send_text(user, "Ivanov Ivan")
    h.send_text(user, "AB1234567")
    calls = h.press(user, f"cz:{COUNTRIES.index('Russia')}")
    if not contacts:
        return calls  # fixed contacts: straight to the final review
    h.send_text(user, "ivan@example.com")
    return h.send_contact(user, "79001234567")


def _complete_checkout(h, user, ids):
    """The rest of a checkout from the category screen on."""
    h.press(user, "p:passenger_car:30d")
    h.press(user, "d:tomorrow")
    h.press(user, "e:manual")
    h.send_text(user, "XY777ZZ")
    h.send_text(user, VIN)
    h.press(user, f"mf:{ids['TOYOTA']}")
    h.press(user, f"md:{ids['TOYOTA/COROLLA']}")
    h.press(user, "vc")
    h.send_text(user, "Petrov Petr")
    h.send_text(user, "CD7654321")
    h.press(user, f"cz:{COUNTRIES.index('Russia')}")
    h.send_text(user, "petr@example.com")
    h.send_contact(user, "79007654321")
    return h.press(user, "fc:continue")


def _new_insurance_button(screen) -> str:
    (data,) = [d for t, d in buttons(screen) if t == texts.BTN_NEW_INSURANCE]
    return data


def _alerts(calls):
    return [c for c in calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]


def _to_documents(h, user):
    _to_method(h, user)
    return h.press(user, "e:documents")


def _photo(h, user, n):
    from test_telegram_bot_documents import photo_bytes

    return h.send_photo(user, photo_bytes(), file_id=f"doc-{n}")


# ------------------------------------------------------ global /start


def _state_date(h, user, ids):
    h.send_text(user, "/start")
    h.press(user, "p:passenger_car:30d")


def _state_method(h, user, ids):
    _to_method(h, user)


def _state_manual_entry(h, user, ids):
    _to_method(h, user)
    h.press(user, "e:manual")
    h.send_text(user, "AB123CD")  # now waiting for the VIN


def _state_manufacturer_search(h, user, ids):
    _to_method(h, user)
    h.press(user, "e:manual")
    h.send_text(user, "AB123CD")
    h.send_text(user, VIN)
    h.press(user, nav("manufacturer"))


def _state_policyholder(h, user, ids):
    _to_vehicle_review(h, user, ids)
    h.press(user, "vc")
    h.send_text(user, "Ivanov Ivan")  # now waiting for the passport


def _state_final_review(h, user, ids):
    _to_final_review(h, user, ids)


@pytest.mark.parametrize(
    "reach",
    [_state_date, _state_method, _state_manual_entry, _state_manufacturer_search, _to_vehicle_review,
     _state_policyholder, _state_final_review],
    ids=lambda f: f.__name__.lstrip("_"),
)
def test_start_works_from_every_checkout_state(harness, settings, ids, reach):
    user = make_user(10)
    reach(harness, user, ids)
    screen = last_screen(harness.send_text(user, "/start"))
    assert screen.text.startswith(texts.INTRO_IN_PROGRESS)
    assert [t for t, _ in buttons(screen)] == [texts.BTN_NEW_INSURANCE, texts.BTN_CONTINUE_CHECKOUT]
    assert last_screen(harness.press(user, _new_insurance_button(screen))).text == CATEGORIES
    draft = _draft(settings, 10)
    for key in ("vehicle_category_code", "period_code", "start_date", "registration_number", "identifier", "full_name"):
        assert key not in draft


def test_start_works_while_photos_are_pending(settings, ids):
    h = BotHarness(settings, ocr_provider=InstantOcrProvider())
    try:
        user = make_user(11)
        _to_documents(h, user)
        _photo(h, user, 1)
        assert _draft(settings, 11)["pending_document_files"]
        screen = last_screen(h.send_text(user, "/start"))
        assert screen.text.startswith(texts.INTRO_IN_PROGRESS)
        h.press(user, _new_insurance_button(screen))
        draft = _draft(settings, 11)
        assert "pending_document_files" not in draft and "document_files" not in draft
    finally:
        h.close()


def test_start_on_a_fresh_session_is_the_plain_intro(harness, settings, ids):
    user = make_user(12)
    screen = last_screen(harness.send_text(user, "/start"))
    assert not screen.text.startswith(texts.INTRO_IN_PROGRESS)
    assert [t for t, _ in buttons(screen)] == ["🚗 Оформить страховку"]
    # nothing chosen yet (only the category screen / a category): still the plain intro
    harness.press(user, "m:apply")
    harness.press(user, "c:passenger_car")
    screen = last_screen(harness.send_text(user, "/start"))
    assert [t for t, _ in buttons(screen)] == ["🚗 Оформить страховку"]
    assert last_screen(harness.press(user, "m:apply")).text == CATEGORIES


def test_continue_checkout_resumes_where_the_customer_was(harness, settings, ids):
    user = make_user(13)
    _to_vehicle_review(harness, user, ids)
    harness.send_text(user, "/start")
    assert last_screen(harness.press(user, nav("resume"))).text.startswith("Проверьте данные автомобиля")
    assert _draft(settings, 13)["registration_number"] == "AB123CD"


# ------------------------------------------------------- cancel + reset


def test_cancel_button_on_every_checkout_screen(harness, settings, ids):
    user = make_user(20)
    assert (texts.BTN_RESTART, nav("restart")) in buttons(last_screen(_to_vehicle_review(harness, user, ids)))
    harness.press(user, "vc")
    harness.send_text(user, "Ivanov Ivan")
    harness.send_text(user, "AB1234567")
    harness.press(user, f"cz:{COUNTRIES.index('Russia')}")
    harness.send_text(user, "ivan@example.com")
    assert (texts.BTN_RESTART, nav("restart")) in buttons(last_screen(harness.send_contact(user, "79001234567")))


def test_cancel_asks_for_confirmation_and_no_keeps_everything(harness, settings, ids):
    user = make_user(21)
    _to_final_review(harness, user, ids)
    confirm = last_screen(harness.press(user, nav("restart")))
    assert confirm.text == texts.RESTART_CONFIRM
    assert buttons(confirm) == [("Да, отменить", nav("restart_confirmed")), ("Нет, продолжить", nav("resume"))]
    assert _draft(settings, 21)["registration_number"] == "AB123CD"  # nothing reset yet
    back = last_screen(harness.press(user, nav("resume")))
    assert back.text.startswith("Проверьте данные:")
    assert _draft(settings, 21)["full_name"]  # still there


def test_cancel_confirmed_resets_everything_but_identity_and_source(harness, settings, ids):
    user = make_user(22, "alice")
    harness.send_text(user, "/start upper_lars")
    _to_final_review(harness, user, ids)
    before = _draft(settings, 22)
    harness.press(user, nav("restart"))
    screen = last_screen(harness.press(user, nav("restart_confirmed")))
    assert screen.text.startswith(texts.CHECKOUT_CANCELLED)
    draft = _draft(settings, 22)
    assert draft["acquisition_source"] == "upper_lars" and draft["telegram_username"] == "alice"
    assert draft["telegram_user_id"] == 22
    assert draft["checkout_gen"] and draft["checkout_gen"] != before.get("checkout_gen")
    for key in ("vehicle_category_code", "period_code", "start_date", "registration_number", "identifier",
                "manufacturer_id", "model_id", "full_name", "passport_number", "contact_email", "contact_phone",
                "checkout_id", "pending_document_files", "document_files", "ocr_batch_seq", "ocr_last_batch_failed"):
        assert key not in draft, key
    # a fresh start really starts fresh: the date step no longer jumps into the old review
    harness.press(user, "m:apply")
    harness.press(user, "p:passenger_car:30d")
    assert last_screen(harness.press(user, "d:tomorrow")).text.startswith("Вы выбрали")


def test_reset_keeps_the_profiles_fixed_contacts(settings, ids):
    h = BotHarness(settings, profile=FIXED)
    try:
        user = make_user(23)
        _to_final_review(h, user, ids, contacts=False)
        screen = last_screen(h.send_text(user, "/start"))
        h.press(user, _new_insurance_button(screen))
        draft = _draft(settings, 23, FIXED)
        assert draft["contact_email"] == "tplgee@mail.ru" and draft["contact_phone"] == "+995 574 22 06 25"
        assert "full_name" not in draft
    finally:
        h.close()


def test_start_payload_after_reset_keeps_deep_link_semantics(harness, settings, ids):
    user = make_user(24)
    _to_vehicle_review(harness, user, ids)
    screen = last_screen(harness.send_text(user, "/start kazbegi"))  # last touch wins, as before
    assert _draft(settings, 24)["acquisition_source"] == "kazbegi"
    harness.press(user, _new_insurance_button(screen))
    assert _draft(settings, 24)["acquisition_source"] == "kazbegi"
    harness.send_text(user, "/start")  # no payload: the source is kept
    assert _draft(settings, 24)["acquisition_source"] == "kazbegi"


# ------------------------------------------------- orders are preserved


def test_reset_never_touches_orders_and_the_next_order_gets_a_new_checkout_id(harness, settings, ids):
    user = make_user(30)
    _to_final_review(harness, user, ids)
    harness.press(user, "fc:continue")
    (first,) = _orders(settings, 30)
    snapshot = _order_rows(settings)

    screen = last_screen(harness.send_text(user, "/start"))
    harness.press(user, _new_insurance_button(screen))
    assert _order_rows(settings) == snapshot  # the reset wrote nothing to insurance_orders
    assert "order_id" not in _draft(settings, 30) and "checkout_id" not in _draft(settings, 30)

    _complete_checkout(harness, user, ids)
    orders = _orders(settings, 30)
    assert len(orders) == 2
    second = next(o for o in orders if o.id != first.id)
    assert second.client_checkout_id and second.client_checkout_id != first.client_checkout_id
    assert _order_rows(settings)[0] == snapshot[0]  # the first order is exactly as it was


def test_a_checkout_id_assigned_before_the_reset_is_never_reused(harness, settings, ids):
    user = make_user(31)
    _to_final_review(harness, user, ids)
    _merge(settings, 31, {"checkout_id": "abandoned-checkout-id"})  # e.g. an order creation that failed half-way
    harness.press(user, nav("restart"))
    harness.press(user, nav("restart_confirmed"))
    harness.press(user, "m:apply")
    _complete_checkout(harness, user, ids)
    (order,) = _orders(settings, 31)
    assert order.client_checkout_id != "abandoned-checkout-id"


def test_restart_confirmed_is_never_an_escape_from_an_existing_order(harness, settings, ids):
    user = make_user(32)
    _to_final_review(harness, user, ids)
    harness.press(user, "fc:continue")
    snapshot = _order_rows(settings)
    screen = last_screen(harness.press(user, nav("restart_confirmed")))  # a stale confirm button
    assert screen.text.startswith("💳 Оплата заказа")
    assert _draft(settings, 32)["order_id"] == snapshot[0]["id"]
    assert _order_rows(settings) == snapshot


# ---------------------------------------- unfinished order: offer, resume


def test_start_with_an_unfinished_order_offers_new_or_back(harness, settings, ids):
    user = make_user(40)
    _to_final_review(harness, user, ids)
    harness.press(user, "fc:continue")
    (order,) = _orders(settings, 40)
    screen = last_screen(harness.send_text(user, "/start"))
    assert screen.text.startswith(texts.INTRO_UNFINISHED_ORDER.format(number=order.public_number))
    assert [t for t, _ in buttons(screen)] == [texts.BTN_NEW_INSURANCE, texts.BTN_BACK_TO_ORDER]
    assert (texts.BTN_BACK_TO_ORDER, f"o:resume:{order.id}") in buttons(screen)
    assert _orders(settings, 40)[0].status == order.status  # /start changed nothing


def test_back_to_order_is_the_old_resume_and_receipts_still_work(harness, settings, ids):
    user = make_user(41)
    _to_final_review(harness, user, ids)
    harness.press(user, "fc:continue")
    (order,) = _orders(settings, 41)
    harness.send_text(user, "/start")
    assert last_screen(harness.press(user, f"o:resume:{order.id}")).text.startswith("💳 Оплата заказа")
    harness.send_photo(user, b"receipt", file_id="rcpt-1")
    assert _orders(settings, 41)[0].status == "payment_review"


def test_new_insurance_next_to_an_unfinished_order_leaves_it_resumable(harness, settings, ids):
    user = make_user(42)
    _to_final_review(harness, user, ids)
    harness.press(user, "fc:continue")
    (order,) = _orders(settings, 42)
    snapshot = _order_rows(settings)
    old_payment_screen_id = harness.next_message_id()

    screen = last_screen(harness.send_text(user, "/start"))
    assert last_screen(harness.press(user, _new_insurance_button(screen))).text == CATEGORIES
    assert "order_id" not in _draft(settings, 42)
    assert _order_rows(settings) == snapshot

    harness.press(user, "p:passenger_car:30d")  # the new checkout has begun
    screen = last_screen(harness.send_text(user, "/start"))
    assert screen.text.startswith(texts.INTRO_UNFINISHED_ORDER.format(number=order.public_number))
    # an order button of an OLDER message still works (orders are never "stale")
    resumed = last_screen(harness.press(user, f"o:resume:{order.id}", message_id=old_payment_screen_id))
    assert resumed.text.startswith("💳 Оплата заказа")
    assert _draft(settings, 42)["order_id"] == order.id
    harness.send_photo(user, b"receipt", file_id="rcpt-2")
    assert _orders(settings, 42)[0].status == "payment_review"


def test_another_customers_order_cannot_be_resumed(harness, settings, ids):
    alice, bob = make_user(43), make_user(44)
    _to_final_review(harness, alice, ids)
    harness.press(alice, "fc:continue")
    (order,) = _orders(settings, 43)
    harness.send_text(bob, "/start")
    harness.press(bob, f"o:resume:{order.id}")
    assert "order_id" not in _draft(settings, 44)


# --------------------------------------------------- stale callbacks


def test_buttons_of_the_abandoned_checkout_are_refused(harness, settings, ids):
    user = make_user(50)
    _to_vehicle_review(harness, user, ids)
    old_review_id = harness.next_message_id()  # the vehicle review message of the old checkout
    screen = last_screen(harness.send_text(user, "/start"))
    harness.press(user, _new_insurance_button(screen))
    fresh = _draft(settings, 50)

    for stale in ("vc", "p:passenger_car:30d", nav("restart_confirmed"), f"md:{ids['TOYOTA/CAMRY']}"):
        calls = harness.press(user, stale, message_id=old_review_id)
        (alert,) = _alerts(calls)
        assert alert.text == texts.STALE_BUTTON
        assert not screens(calls)
    assert _draft(settings, 50) == fresh  # nothing written into the fresh checkout


def test_double_new_insurance_press_resets_only_once(harness, settings, ids):
    user = make_user(51)
    _to_vehicle_review(harness, user, ids)
    screen = last_screen(harness.send_text(user, "/start"))
    button = _new_insurance_button(screen)
    harness.press(user, button)
    harness.press(user, "p:passenger_car:30d")  # progress in the fresh checkout
    gen = _draft(settings, 51)["checkout_gen"]
    harness.press(user, button)  # the same (now spent) button again
    draft = _draft(settings, 51)
    assert draft["checkout_gen"] == gen and draft["vehicle_category_code"] == "passenger_car"


# ------------------------------------------------ late OCR / album results


def _blocking_harness(settings, provider):
    h = BotHarness(settings, ocr_provider=provider)
    h.drain_background = False  # updates return while the batch still runs
    return h


def test_start_is_answered_while_ocr_runs_and_the_late_result_is_discarded(settings, ids):
    provider = BlockingOcrProvider()
    h = _blocking_harness(settings, provider)
    try:
        user = make_user(60)
        _to_documents(h, user)
        for n in (1, 2, 3):
            _photo(h, user, n)  # the 3rd starts the batch -- in the background
        assert h.pump_until(provider.entered.is_set)

        # /start is answered right away (the batch doesn't hold the customer's lock)
        screen = last_screen(h.send_text(user, "/start"))
        assert screen.text.startswith(texts.INTRO_IN_PROGRESS)
        assert last_screen(h.press(user, _new_insurance_button(screen))).text == CATEGORIES
        fresh = _draft(settings, 60)

        provider.release.set()
        calls = h.settle()
        draft = _draft(settings, 60)
        assert draft == fresh  # the late result wrote nothing
        assert "registration_number" not in draft and "document_files" not in draft
        assert not any(c.text.startswith("✅ Документы распознаны") for c in screens(calls))
        assert any(isinstance(c, EditMessageText) and c.text == texts.OCR_CANCELLED for c in calls)
        assert _events(settings, "bot_ocr_discarded") == 1
        assert _events(settings, "bot_ocr_completed") == 0
    finally:
        provider.release.set()
        h.close()


def test_late_result_after_cancel_confirmed_is_discarded(settings, ids):
    provider = BlockingOcrProvider()
    h = _blocking_harness(settings, provider)
    try:
        user = make_user(61)
        _to_documents(h, user)
        for n in (1, 2, 3):
            _photo(h, user, n)
        assert h.pump_until(provider.entered.is_set)
        h.press(user, nav("restart"))
        h.press(user, nav("restart_confirmed"))
        h.press(user, "m:apply")
        h.press(user, "p:passenger_car:30d")
        fresh = _draft(settings, 61)
        provider.release.set()
        h.settle()
        assert _draft(settings, 61) == fresh
    finally:
        provider.release.set()
        h.close()


def test_a_result_still_applies_when_nothing_was_reset(settings, ids):
    provider = BlockingOcrProvider()
    h = _blocking_harness(settings, provider)
    try:
        user = make_user(62)
        _to_documents(h, user)
        for n in (1, 2, 3):
            _photo(h, user, n)
        assert h.pump_until(provider.entered.is_set)
        provider.release.set()
        calls = h.settle()
        assert any(c.text.startswith("✅ Документы распознаны") for c in screens(calls))
        assert _draft(settings, 62)["registration_number"] == "AB123CD"
    finally:
        provider.release.set()
        h.close()


def test_a_second_batch_never_starts_while_one_runs(settings, ids):
    provider = BlockingOcrProvider()
    h = _blocking_harness(settings, provider)
    try:
        user = make_user(63)
        _to_documents(h, user)
        for n in (1, 2, 3):
            _photo(h, user, n)
        assert h.pump_until(provider.entered.is_set)
        h.press(user, "dc:done")  # a queued "done" press while the batch runs
        provider.release.set()
        h.settle()
        assert provider.calls == 1
    finally:
        provider.release.set()
        h.close()


def test_late_album_of_the_abandoned_checkout_is_discarded(settings, ids):
    from test_telegram_bot_documents import photo_bytes

    provider = BlockingOcrProvider()
    h = _blocking_harness(settings, provider)
    try:
        user = make_user(64)
        _to_documents(h, user)
        items = [("photo", f"alb-{n}", photo_bytes()) for n in (1, 2, 3)]
        h.send_album(user, "album-1", items, flush=False)  # still collecting
        screen = last_screen(h.send_text(user, "/start"))
        h.press(user, _new_insurance_button(screen))
        fresh = _draft(settings, 64)
        provider.release.set()
        h.settle()
        assert _draft(settings, 64) == fresh
        assert _events(settings, "bot_ocr_completed") == 0
    finally:
        provider.release.set()
        h.close()
