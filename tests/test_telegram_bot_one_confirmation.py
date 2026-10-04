"""ONE final confirmation: "✅ Всё верно" on the consolidated review creates
the order and shows the payment details right away -- there is no second
"Проверьте заявку" screen and no "✅ Продолжить" button any more, on the
OCR path, the manual path, and after a single-field edit.

Then the (still manual) payment check: the receipt goes to THIS customer's
order, the manager sees what to look for in the bank (amount, recipient,
the receipt itself) and confirms or rejects; the existing policy flow
follows only an approved payment.

A profile with fixed contacts (as @OSAGO24GEbot) and payment requisites in
the format the real .env uses. All offline: fake Bot API, fake OCR, tmp DB."""

import asyncio
import dataclasses
import io
from datetime import datetime, timezone

import pytest
from aiogram.methods import EditMessageText, SendDocument, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update
from PIL import Image

from app.countries import COUNTRIES
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.ocr.models import OcrResult
from app.ocr.provider import OcrProvider
from app.orders import files as order_files
from app.orders.repository import get_order_by_id, list_telegram_orders
from app.orders.state_machine import OrderStatus
from app.pricing import overrides as price_overrides
from app.sessions.repository import merge_draft
from app.settings import load_settings
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, nav, screens, seed_catalog, sent_to

VIN = "WVWZZZ1JZXW000001"
MANAGER = 999
FIXED = dataclasses.replace(TEST_PROFILE, customer_email="tplgee@mail.ru", customer_phone="+995 574 22 06 25")
RESULT = OcrResult(
    provider="fake", registration_number="AB123CD", vin=VIN, chassis_number=None, manufacturer="Toyota", model="Camry",
    policyholder_full_name="PETROV PETR", passport_number="751234567", citizenship="Russian Federation",
)
OLD_SCREENS = ("Проверьте заявку",)
OLD_BUTTONS = ("✅ Продолжить",)
CONTACT_PROMPTS = ("✉️ Email", "📱 Телефон")


class Provider(OcrProvider):
    def recognize(self, images):
        return RESULT


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    # exactly the format of the local .env values
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", "+79495205223")
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", "Владимир М.")
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def ids(settings):
    ids = seed_catalog(settings.app.db_file)
    conn = get_connection(settings.app.db_file)
    try:  # an admin price, so the amount is provably the order's own (not a constant)
        price_overrides.upsert_override(
            conn, country_code="GE", vehicle_category_code="passenger_car", period_code="30d", price_rub=1349, updated_by="t"
        )
        conn.commit()
    finally:
        conn.close()
    return ids


@pytest.fixture
def h(settings, ids):
    harness = BotHarness(settings, profile=FIXED, manager_ids={MANAGER}, ocr_provider=Provider())
    yield harness
    harness.close()


# ------------------------------------------------------------------ helpers


def _orders(settings, user_id):
    conn = get_connection(settings.app.db_file)
    try:
        return list_telegram_orders(conn, bot_key=FIXED.bot_key, telegram_user_id=user_id)
    finally:
        conn.close()


def _order(settings, order_id):
    conn = get_connection(settings.app.db_file)
    try:
        return get_order_by_id(conn, order_id)
    finally:
        conn.close()


def _order_count(settings):
    conn = get_connection(settings.app.db_file)
    try:
        return conn.execute("SELECT COUNT(*) FROM insurance_orders").fetchone()[0]
    finally:
        conn.close()


def _receipts(settings, order_id):
    conn = get_connection(settings.app.db_file)
    try:
        return order_files.list_files(conn, order_id, order_files.KIND_PAYMENT_RECEIPT)
    finally:
        conn.close()


def _photo() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1000, 700), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _ocr_review(h, user):
    h.send_text(user, "/start")
    h.press(user, "p:passenger_car:30d")
    h.press(user, "d:tomorrow")
    h.press(user, "e:documents")
    return h.send_album(user, f"grp-{user.id}", [("photo", f"f{user.id}-{i}", _photo()) for i in range(3)])


def _manual_review(h, user, ids):
    calls = []
    for step in (
        lambda: h.send_text(user, "/start"),
        lambda: h.press(user, "p:passenger_car:30d"),
        lambda: h.press(user, "d:tomorrow"),
        lambda: h.press(user, "e:manual"),
        lambda: h.send_text(user, "AB123CD"),
        lambda: h.send_text(user, VIN),
        lambda: h.press(user, f"mf:{ids['TOYOTA']}"),
        lambda: h.press(user, f"md:{ids['TOYOTA/CAMRY']}"),
        lambda: h.press(user, "vc"),
        lambda: h.send_text(user, "Ivanov Ivan"),
        lambda: h.send_text(user, "AB1234567"),
        lambda: h.press(user, f"cz:{COUNTRIES.index('Russia')}"),
    ):
        calls += step()
    return calls


def _texts(calls):
    return [c.text for c in screens(calls)]


def _no_second_confirmation(calls):
    for call in screens(calls):
        assert not call.text.startswith(OLD_SCREENS), call.text
        assert not any(text in OLD_BUTTONS for text, _ in buttons(call))


def _expected_payment(order) -> str:
    return (
        f"💳 Оплата заказа {order.public_number}\n\n"
        "Сумма: 1 349 ₽\n\n"
        "Банк: Сбербанк\n"
        "Перевод по номеру телефона:\n"
        "+7 949 520-52-23\n\n"
        "Получатель: Владимир М.\n\n"
        "После оплаты отправьте сюда скриншот или фото чека (можно PDF)."
    )


def _confirmed(h, settings, user):
    calls = h.press(user, "fc:confirm")
    (order,) = _orders(settings, user.id)
    return order, calls


# ------------------------------------------------- one confirmation


def test_ocr_review_all_correct_goes_straight_to_payment(h, settings, ids):
    user = make_user(1)
    review_calls = _ocr_review(h, user)
    review = last_screen(review_calls)
    assert review.text.startswith("✅ Документы распознаны")
    assert buttons(review)[0] == ("✅ Всё верно", "fc:confirm")
    assert _order_count(settings) == 0  # nothing ordered before the confirmation

    order, calls = _confirmed(h, settings, user)
    assert [c.text for c in screens(calls)] == [_expected_payment(order)]  # the ONLY screen after "✅ Всё верно"
    assert [t for t, _ in buttons(last_screen(calls))] == ["📎 Отправить чек", "🔄 Показать реквизиты ещё раз"]
    _no_second_confirmation(review_calls + calls)
    assert order.status == OrderStatus.AWAITING_PAYMENT.value
    assert order.price_customer_minor == 134900  # the order's own amount -- the one shown


def test_manual_review_all_correct_goes_straight_to_payment(h, settings, ids):
    user = make_user(2)
    review_calls = _manual_review(h, user, ids)
    review = last_screen(review_calls)
    assert review.text.startswith("Проверьте данные:")
    assert "ФИО: Ivanov Ivan" in review.text and "Стоимость: 1 349 ₽" in review.text
    order, calls = _confirmed(h, settings, user)
    assert [c.text for c in screens(calls)] == [_expected_payment(order)]
    _no_second_confirmation(review_calls + calls)


def test_edit_a_field_then_all_correct_goes_to_payment(h, settings, ids):
    user = make_user(3)
    _ocr_review(h, user)
    h.press(user, nav("passport", "checkout_review"))
    review = last_screen(h.send_text(user, "C01234567"))
    assert review.text.startswith("✅ Документы распознаны") and "Паспорт: C01234567" in review.text
    h.press(user, nav("plate", "checkout_review"))
    review = last_screen(h.send_text(user, "XY777ZZ"))
    assert "Госномер: XY777ZZ" in review.text
    assert _order_count(settings) == 0
    order, calls = _confirmed(h, settings, user)
    assert [c.text for c in screens(calls)] == [_expected_payment(order)]
    assert (order.identification_number, order.display_registration_number) == ("C01234567", "XY777ZZ")


def _merge(settings, user_id, updates):
    conn = get_connection(settings.app.db_file)
    try:
        merge_draft(conn, session_id_for(FIXED.bot_key, user_id), updates)
    finally:
        conn.close()


def test_all_correct_with_an_incomplete_vehicle_creates_nothing(h, settings, ids):
    user = make_user(4)
    _manual_review(h, user, ids)
    _merge(settings, 4, {"model_id": None})  # e.g. the model left the catalog
    calls = h.press(user, "fc:confirm")
    assert any(getattr(c, "show_alert", False) for c in calls)
    assert last_screen(calls).text.startswith("Проверьте данные:")
    assert _order_count(settings) == 0


def test_all_correct_with_a_missing_policyholder_field_asks_for_it_then_back_to_review(h, settings, ids):
    user = make_user(9)
    _manual_review(h, user, ids)
    _merge(settings, 9, {"full_name": None})
    asked = last_screen(h.press(user, "fc:confirm"))
    assert asked.text.startswith("👤 ФИО")
    assert _order_count(settings) == 0
    review = last_screen(h.send_text(user, "Ivanov Ivan"))
    assert review.text.startswith("Проверьте данные:")  # back on the review, nothing else asked
    order, calls = _confirmed(h, settings, user)
    assert [c.text for c in screens(calls)] == [_expected_payment(order)]


def test_customer_is_never_asked_for_email_or_phone(h, settings, ids):
    user = make_user(5)
    calls = _ocr_review(h, user) + h.press(user, "fc:confirm")
    calls += _manual_review(h, make_user(6), ids) + h.press(make_user(6), "fc:confirm")
    assert not any(t.startswith(CONTACT_PROMPTS) for t in _texts(calls))
    for uid in (5, 6):
        (order,) = _orders(settings, uid)
        assert (order.contact_email, order.contact_phone) == ("tplgee@mail.ru", "+995 574 22 06 25")


# ---------------------------------------------------------- idempotency


def test_order_is_created_exactly_once_and_a_repeat_press_shows_it(h, settings, ids):
    user = make_user(7)
    _ocr_review(h, user)
    order, _ = _confirmed(h, settings, user)
    again = h.press(user, "fc:confirm")  # a double tap / replayed callback
    assert last_screen(again).text == _expected_payment(order)
    old = h.press(user, "fc:continue")  # the former "✅ Продолжить" of an older message
    assert last_screen(old).text == _expected_payment(order)
    assert _order_count(settings) == 1
    assert _order(settings, order.id).status == OrderStatus.AWAITING_PAYMENT.value


def test_simultaneous_duplicate_callbacks_create_one_order(h, settings, ids):
    user = make_user(8)
    _ocr_review(h, user)
    message_id = h.next_message_id()

    def update(n):
        message = Message(message_id=message_id, date=datetime.now(timezone.utc), chat=Chat(id=user.id, type="private"), text="…")
        callback = CallbackQuery(id=f"dup-{n}", from_user=user, chat_instance="ci", data="fc:confirm", message=message)
        return Update(update_id=10_000 + n, callback_query=callback)

    async def both():
        await asyncio.gather(*(h.dispatcher.feed_update(h.bot, update(n)) for n in (1, 2)))

    h.loop.run_until_complete(both())
    assert _order_count(settings) == 1


# --------------------------------------------------------------- receipts


def _to_receipt(h, settings, user, file_id="rcpt-1"):
    _ocr_review(h, user)
    order, _ = _confirmed(h, settings, user)
    return order, h.send_photo(user, b"receipt-bytes", file_id=file_id)


def test_receipt_goes_to_this_customers_order_only(h, settings, ids):
    alice, bob = make_user(10, "alice"), make_user(11, "bob")
    _ocr_review(h, alice)
    alice_order, _ = _confirmed(h, settings, alice)
    bob_order, _ = _to_receipt(h, settings, bob, file_id="rcpt-bob")
    assert [r.telegram_file_id for r in _receipts(settings, bob_order.id)] == ["rcpt-bob"]
    assert _receipts(settings, alice_order.id) == []
    assert _order(settings, alice_order.id).status == OrderStatus.AWAITING_PAYMENT.value
    assert _order(settings, bob_order.id).status == OrderStatus.PAYMENT_REVIEW.value
    # bob can't open alice's order with a crafted button
    h.press(bob, f"o:receipt:{alice_order.id}")
    h.send_photo(bob, b"x", file_id="rcpt-bob-2")
    assert _receipts(settings, alice_order.id) == []


def test_photo_before_any_order_is_never_a_receipt(h, settings, ids):
    user = make_user(12)
    h.send_text(user, "/start")
    calls = h.send_photo(user, b"not-a-receipt", file_id="early")
    assert _order_count(settings) == 0
    assert not sent_to(calls, MANAGER)  # nothing reaches a manager
    assert screens(calls)  # the customer still gets an answer


def test_manager_gets_receipt_with_amount_recipient_and_order(h, settings, ids):
    user = make_user(13, "client")
    order, calls = _to_receipt(h, settings, user)
    card = sent_to(calls, MANAGER, SendMessage)[0]
    for fragment in (
        f"🆕 Новая заявка {order.public_number}", "ФИО PETROV PETR", "Сумма к получению: 1 349 ₽",
        "Получатель: Владимир М.", "Сбербанк · +7 949 520-52-23", "💰 1 349 ₽",
    ):
        assert fragment in card.text, fragment
    assert buttons(card)[:2] == [("✅ Оплата поступила", f"mg:confirm:{order.id}"), ("❌ Оплата не поступила", f"mg:reject:{order.id}")]
    photos = sent_to(calls, MANAGER, SendPhoto)
    assert len(photos) == 4  # the 3 vehicle documents + the receipt
    (photo,) = [p for p in photos if p.photo == "rcpt-1"]
    assert photo.caption == f"🧾 Чек об оплате · {order.public_number}\nОжидается: 1 349 ₽ · PETROV PETR"


def test_manager_confirm_then_the_existing_policy_flow(h, settings, ids):
    user = make_user(14)
    order, _ = _to_receipt(h, settings, user)
    calls = h.press(make_user(MANAGER), f"mg:confirm:{order.id}")
    assert _order(settings, order.id).status == OrderStatus.PAID.value
    (notice,) = sent_to(calls, 14, SendMessage)
    assert notice.text.startswith("✅ Оплата подтверждена")
    h.press(make_user(MANAGER), f"mg:policy:{order.id}")
    calls = h.send_document(make_user(MANAGER), b"%PDF-1.4 fake", file_id="policy-1", mime_type="application/pdf", file_name="p.pdf")
    (delivered,) = sent_to(calls, 14, SendDocument)
    assert delivered.document == "policy-1"
    assert _order(settings, order.id).status == OrderStatus.POLICY_READY.value


def test_no_policy_before_the_payment_is_approved(h, settings, ids):
    user = make_user(15)
    order, _ = _to_receipt(h, settings, user)
    h.press(make_user(MANAGER), f"mg:policy:{order.id}")  # not allowed in payment review
    calls = h.send_document(make_user(MANAGER), b"%PDF-1.4 fake", file_id="policy-x", mime_type="application/pdf", file_name="p.pdf")
    assert not sent_to(calls, 15, SendDocument)
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value


def test_manager_reject_then_resubmission(h, settings, ids):
    user = make_user(16)
    order, _ = _to_receipt(h, settings, user)
    calls = h.press(make_user(MANAGER), f"mg:reject:{order.id}")
    assert _order(settings, order.id).status == OrderStatus.AWAITING_PAYMENT.value
    (notice,) = sent_to(calls, 16, SendMessage)
    assert notice.text.startswith("❌ Платёж пока не найден")
    for fragment in ("Сумма: 1 349 ₽", "+7 949 520-52-23", "Получатель: Владимир М."):
        assert fragment in notice.text
    assert any(isinstance(c, EditMessageText) and c.chat_id == MANAGER for c in calls)

    calls = h.send_photo(user, b"receipt-2", file_id="rcpt-2")
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value
    assert "rcpt-2" in [p.photo for p in sent_to(calls, MANAGER, SendPhoto)]
    assert len(_receipts(settings, order.id)) == 2
    assert _order_count(settings) == 1
