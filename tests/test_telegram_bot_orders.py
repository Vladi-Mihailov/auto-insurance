"""Phase 5: order creation, Sber-by-phone payment details, receipts, the
manager review/confirm/reject flow, policy upload + delivery, and customer
resume -- all through the real Dispatcher with the offline harness (no
Telegram, no OpenAI, no bank). Real config.yaml pricing; fake payment
details from env; a fresh SQLite file per test."""

import json
import logging

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage, SendPhoto

from app.countries import COUNTRIES
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.notifications import bot_outbox
from app.orders import files as order_files
from app.orders.repository import get_order_by_id, list_telegram_orders
from app.orders.state_machine import OrderStatus
from app.pricing import overrides as price_overrides
from app.sessions.repository import get_draft, merge_draft
from app.settings import load_settings
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import (
    FAKE_TOKEN,
    TEST_PROFILE,
    BotHarness,
    buttons,
    last_screen,
    make_outbox_due,
    make_user,
    seed_catalog,
    sent_to,
)

VIN = "WVWZZZ1JZXW000001"
MANAGER, MANAGER_B = 999, 998
FAKE_PHONE = "+7 900 000-00-00"
FAKE_RECIPIENT = "Тестов Т."


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", FAKE_PHONE)
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", FAKE_RECIPIENT)
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


@pytest.fixture
def harness(settings, ids):
    h = BotHarness(settings, manager_ids={MANAGER})
    yield h
    h.close()


@pytest.fixture
def two_managers(settings, ids):
    h = BotHarness(settings, manager_ids={MANAGER, MANAGER_B})
    yield h
    h.close()


# ------------------------------------------------------------------ helpers


def _conn(settings):
    return get_connection(settings.app.db_file)


def _draft(settings, user_id):
    conn = _conn(settings)
    try:
        return get_draft(conn, session_id_for(TEST_PROFILE.bot_key, user_id)) or {}
    finally:
        conn.close()


def _orders(settings, user_id):
    conn = _conn(settings)
    try:
        return list_telegram_orders(conn, bot_key=TEST_PROFILE.bot_key, telegram_user_id=user_id)
    finally:
        conn.close()


def _order(settings, order_id):
    conn = _conn(settings)
    try:
        return get_order_by_id(conn, order_id)
    finally:
        conn.close()


def _files(settings, order_id, kind=None):
    conn = _conn(settings)
    try:
        return order_files.list_files(conn, order_id, kind)
    finally:
        conn.close()


def _history(settings, order_id):
    conn = _conn(settings)
    try:
        rows = conn.execute(
            "SELECT from_status, to_status FROM insurance_order_status_history WHERE order_id = ? ORDER BY id", (order_id,)
        ).fetchall()
    finally:
        conn.close()
    return [(r["from_status"], r["to_status"]) for r in rows]


def _events(settings, name):
    conn = _conn(settings)
    try:
        rows = conn.execute("SELECT properties FROM insurance_analytics_events WHERE event_name = ? ORDER BY id", (name,)).fetchall()
    finally:
        conn.close()
    return [json.loads(r["properties"]) if r["properties"] else None for r in rows]


def _all_event_json(settings) -> str:
    conn = _conn(settings)
    try:
        rows = conn.execute("SELECT event_name, properties FROM insurance_analytics_events").fetchall()
    finally:
        conn.close()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


def _to_final_review(h, user, ids, *, source=None):
    h.send_text(user, f"/start {source}" if source else "/start")
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
    return h.send_contact(user, "79001234567")


def _place_order(h, user, ids, **kwargs):
    _to_final_review(h, user, ids, **kwargs)
    calls = h.press(user, "fc:continue")
    (order,) = _orders(h.dispatcher["settings"], user.id)
    return order, calls


def _receipt_photo(h, user, file_id="rcpt-1"):
    return h.send_photo(user, b"receipt-bytes-never-downloaded", file_id=file_id)


def _to_review(h, user, ids, **kwargs):
    order, _ = _place_order(h, user, ids, **kwargs)
    calls = _receipt_photo(h, user)
    return order, calls


def _to_paid(h, user, ids, manager=MANAGER):
    order, _ = _to_review(h, user, ids)
    h.press(make_user(manager), f"mg:confirm:{order.id}")
    return order


def _policy_pdf(h, manager_id, file_id="policy-1", mime="application/pdf", name="policy.pdf"):
    return h.send_document(make_user(manager_id), b"%PDF-1.4 fake", file_id=file_id, mime_type=mime, file_name=name)


# -------------------------------------------------------------------- ORDER


def test_continue_creates_one_order_with_all_metadata(harness, settings, ids):
    user = make_user(1, "alice")
    order, calls = _place_order(harness, user, ids, source="upper_lars")
    assert order.channel == "telegram" and order.bot_key == TEST_PROFILE.bot_key
    assert (order.telegram_user_id, order.telegram_chat_id, order.telegram_username) == (1, 1, "alice")
    assert order.acquisition_source == "upper_lars"
    assert order.status == OrderStatus.AWAITING_PAYMENT.value
    assert order.price_customer_minor == 214900
    assert (order.car_number, order.identifier_type, order.identifier) == ("AB123CD", "vin", VIN)
    assert (order.vehicle_make, order.vehicle_model) == ("TOYOTA", "CAMRY")
    assert (order.full_name, order.identification_number, order.citizenship) == ("Ivanov Ivan", "AB1234567", "Russia")
    assert (order.contact_email, order.contact_phone, order.contact_telegram) == ("ivan@example.com", "+79001234567", "@alice")
    assert order.driver_same_as_policyholder and order.owner_same_as_policyholder
    assert order.client_checkout_id
    assert _history(settings, order.id) == [
        (None, "draft"), ("draft", "data_completed"), ("data_completed", "awaiting_payment")
    ]
    draft = _draft(settings, 1)
    assert draft["order_id"] == order.id and draft["acquisition_source"] == "upper_lars"
    assert "registration_number" not in draft and "checkout_id" not in draft
    (created,) = _events(settings, "bot_order_created")
    assert created == {
        "order_id": order.id, "bot_key": "testbot", "acquisition_source": "upper_lars",
        "category": "passenger_car", "period": "30d", "amount_rub": 2149,
    }


def test_double_continue_creates_one_order(harness, settings, ids):
    user = make_user(2)
    _to_final_review(harness, user, ids)
    first = last_screen(harness.press(user, "fc:continue"))
    second = last_screen(harness.press(user, "fc:continue"))
    assert len(_orders(settings, 2)) == 1
    assert first.text == second.text and first.text.startswith("💳 Оплата заказа")


def test_replay_after_crash_mid_creation_creates_no_duplicate(settings, ids):
    """Simulate a crash right after the order row was written (draft not
    finalized, status still DATA_COMPLETED), then a restarted bot receiving
    the same "Продолжить" again."""
    from app.checkout import service as checkout_service

    user = make_user(3)
    first = BotHarness(settings, manager_ids={MANAGER})
    _to_final_review(first, user, ids)
    first.close()
    conn = _conn(settings)
    try:
        session_id = session_id_for(TEST_PROFILE.bot_key, 3)
        merge_draft(conn, session_id, {"checkout_id": "crash-checkout"})
        checkout_service.create_order_from_draft(
            conn, settings, session_id=session_id,
            policyholder={"full_name": "Ivanov Ivan", "identification_number": "AB1234567", "citizenship": "Russia",
                          "contact_email": "ivan@example.com", "contact_phone": "+79001234567",
                          "contact_telegram": None, "contact_max": None, "contact_other": None},
            channel="telegram", bot_key=TEST_PROFILE.bot_key, telegram_user_id=3, telegram_chat_id=3,
            client_checkout_id="crash-checkout",
        )
    finally:
        conn.close()
    restarted = BotHarness(settings, manager_ids={MANAGER})
    try:
        screen = last_screen(restarted.press(user, "fc:continue"))
        restarted.press(user, "fc:continue")
    finally:
        restarted.close()
    (order,) = _orders(settings, 3)
    assert order.status == OrderStatus.AWAITING_PAYMENT.value
    assert _draft(settings, 3)["order_id"] == order.id
    assert screen.text.startswith("💳 Оплата заказа")


def test_order_uses_the_current_price_then_keeps_it(harness, settings, ids):
    user = make_user(4)
    _to_final_review(harness, user, ids)
    conn = _conn(settings)
    try:
        price_overrides.upsert_override(conn, country_code="GE", vehicle_category_code="passenger_car", period_code="30d", price_rub=2555, updated_by="t")
        conn.commit()
    finally:
        conn.close()
    calls = harness.press(user, "fc:continue")
    (order,) = _orders(settings, 4)
    assert order.price_customer_minor == 255500  # re-read at creation
    assert "Сумма: 2 555 ₽" in last_screen(calls).text
    conn = _conn(settings)
    try:
        price_overrides.upsert_override(conn, country_code="GE", vehicle_category_code="passenger_car", period_code="30d", price_rub=9999, updated_by="t")
        conn.commit()
    finally:
        conn.close()
    # stored amount, never the new price
    harness.send_text(user, "/start")
    assert "Сумма: 2 555 ₽" in last_screen(harness.press(user, f"o:resume:{order.id}")).text
    assert _order(settings, order.id).price_customer_minor == 255500


def test_vehicle_documents_are_attached_to_the_order(harness, settings, ids):
    user = make_user(5)
    _to_final_review(harness, user, ids)
    conn = _conn(settings)
    try:
        merge_draft(conn, session_id_for(TEST_PROFILE.bot_key, 5), {"document_files": [
            {"file_id": "doc-a", "file_unique_id": "u-doc-a", "kind": "photo", "mime_type": "image/jpeg", "file_size": 10},
            {"file_id": "doc-b", "file_unique_id": "u-doc-b", "kind": "document", "mime_type": "image/png", "file_size": 20},
        ]})
    finally:
        conn.close()
    harness.press(user, "fc:continue")
    (order,) = _orders(settings, 5)
    docs = _files(settings, order.id, order_files.KIND_VEHICLE_DOCUMENT)
    assert [(d.telegram_file_id, d.telegram_media_type) for d in docs] == [("doc-a", "photo"), ("doc-b", "document")]


def test_incomplete_draft_cannot_be_ordered_by_a_crafted_continue(harness, settings, ids):
    user = make_user(6)
    harness.send_text(user, "/start")
    harness.press(user, "p:passenger_car:30d")
    harness.press(user, "fc:continue")
    assert _orders(settings, 6) == []


# ----------------------------------------------------------- PAYMENT SCREEN


def test_payment_screen_shows_configured_details(harness, settings, ids):
    order, calls = _place_order(harness, make_user(10), ids)
    screen = last_screen(calls)
    assert screen.text == (
        f"💳 Оплата заказа {order.public_number}\n\n"
        "Сумма: 2 149 ₽\n\nБанк: Сбербанк\nПеревод по номеру телефона:\n"
        f"{FAKE_PHONE}\n\nПолучатель: {FAKE_RECIPIENT}\n\n"
        "После оплаты отправьте сюда скриншот или фото чека (можно PDF)."
    )
    assert [t for t, _ in buttons(screen)] == ["📎 Отправить чек", "🔄 Показать реквизиты ещё раз"]
    again = last_screen(harness.press(make_user(10), f"o:details:{order.id}"))
    assert FAKE_PHONE in again.text
    assert _events(settings, "bot_payment_details_shown")[0]["available"] is True


def test_incomplete_payment_config_fails_safely(settings, ids, monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_PAYMENT_RECIPIENT")
    partial = load_settings(PROJECT_ROOT)
    h = BotHarness(partial, manager_ids={MANAGER})
    try:
        with caplog.at_level(logging.WARNING):
            order, calls = _place_order(h, make_user(11), ids)
    finally:
        h.close()
    screen = last_screen(calls)
    assert screen.text == f"⏳ Заказ {order.public_number} создан.\n\nОплата временно недоступна — менеджер свяжется с вами в этом чате."
    assert FAKE_PHONE not in screen.text and "Сбербанк" not in screen.text
    assert order.status == OrderStatus.AWAITING_PAYMENT.value
    assert "missing TELEGRAM_PAYMENT_RECIPIENT" in caplog.text
    assert FAKE_PHONE not in caplog.text


def test_payment_settings_never_render_secrets(settings):
    for rendered in (repr(settings), repr(settings.telegram_payment), str(settings.telegram_payment)):
        assert FAKE_PHONE not in rendered and FAKE_RECIPIENT not in rendered


# ----------------------------------------------------------------- RECEIPTS


def test_photo_receipt_starts_review_and_notifies_manager(harness, settings, ids):
    user = make_user(20, "bob")
    order, _ = _place_order(harness, user, ids, source="google")
    calls = _receipt_photo(harness, user)
    assert last_screen([c for c in calls if getattr(c, "chat_id", None) == 20]).text.startswith("🧾 Чек по заказу")
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value
    (receipt,) = _files(settings, order.id, order_files.KIND_PAYMENT_RECEIPT)
    assert (receipt.telegram_media_type, receipt.mime_type) == ("photo", "image/jpeg")

    card = sent_to(calls, MANAGER, SendMessage)[0]
    assert card.text.startswith(f"🆕 Новая заявка {order.public_number}")
    for fragment in ("@bob · Telegram ID 20", "🚗 Легковой автомобиль", "⏱ 30 дней", "💰 2 149 ₽", "Госномер AB123CD",
                     f"VIN {VIN}", "Марка TOYOTA", "Модель CAMRY", "ФИО Ivanov Ivan", "Паспорт AB1234567",
                     "Телефон +79001234567", "google", "⏳ Ожидает проверки оплаты"):
        assert fragment in card.text
    assert [b[1] for b in buttons(card)][:2] == [f"mg:confirm:{order.id}", f"mg:reject:{order.id}"]
    photo = sent_to(calls, MANAGER, SendPhoto)[0]
    assert photo.photo == "rcpt-1" and "Чек об оплате" in photo.caption
    assert photo.reply_parameters.message_id is not None  # threaded under the card
    assert _events(settings, "bot_payment_review_started")[0]["order_id"] == order.id


@pytest.mark.parametrize(("mime", "name"), [("image/png", "check.png"), ("application/pdf", "check.pdf")])
def test_image_document_and_pdf_receipts_accepted(harness, settings, ids, mime, name):
    user = make_user(21)
    order, _ = _place_order(harness, user, ids)
    calls = harness.send_document(user, b"x", file_id="rcpt-doc", mime_type=mime, file_name=name)
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value
    assert sent_to(calls, MANAGER, SendDocument)[0].document == "rcpt-doc"


def test_invalid_receipt_type_rejected(harness, settings, ids):
    user = make_user(22)
    order, _ = _place_order(harness, user, ids)
    screen = last_screen(harness.send_document(user, b"PK", file_id="zip", mime_type="application/zip", file_name="a.zip"))
    assert "не подходит" in screen.text
    assert _files(settings, order.id, order_files.KIND_PAYMENT_RECEIPT) == []
    assert _order(settings, order.id).status == OrderStatus.AWAITING_PAYMENT.value


def test_oversized_receipt_rejected(harness, settings, ids):
    user = make_user(23)
    order, _ = _place_order(harness, user, ids)
    screen = last_screen(harness.send_photo(user, b"x", file_id="huge", file_size=25 * 1024 * 1024))
    assert "слишком большой" in screen.text
    assert _order(settings, order.id).status == OrderStatus.AWAITING_PAYMENT.value


def test_same_receipt_twice_is_a_no_op(harness, settings, ids):
    user = make_user(24)
    order, _ = _to_review(harness, user, ids)
    calls = _receipt_photo(harness, user)
    assert "уже получен" in last_screen(calls).text
    assert len(_files(settings, order.id, order_files.KIND_PAYMENT_RECEIPT)) == 1
    assert sent_to(calls, MANAGER) == []


def test_second_receipt_during_review_is_forwarded_without_a_new_round(harness, settings, ids):
    user = make_user(25)
    order, _ = _to_review(harness, user, ids)
    calls = _receipt_photo(harness, user, file_id="rcpt-2")
    assert "Дополнительный чек" in last_screen([c for c in calls if getattr(c, "chat_id", None) == 25]).text
    extra = sent_to(calls, MANAGER, SendPhoto)
    assert len(extra) == 1 and extra[0].photo == "rcpt-2" and "Дополнительный чек" in extra[0].caption
    assert sent_to(calls, MANAGER, SendMessage) == []  # no second card
    assert len(_files(settings, order.id, order_files.KIND_PAYMENT_RECEIPT)) == 2
    assert _history(settings, order.id).count(("awaiting_payment", "payment_review")) == 1
    assert len(_orders(settings, 25)) == 1


def test_receipt_after_payment_confirmed_changes_nothing(harness, settings, ids):
    user = make_user(26)
    order = _to_paid(harness, user, ids)
    screen = last_screen(_receipt_photo(harness, user, file_id="late"))
    assert "уже подтверждена" in screen.text
    assert _order(settings, order.id).status == OrderStatus.PAID.value
    assert len(_files(settings, order.id, order_files.KIND_PAYMENT_RECEIPT)) == 1


# ------------------------------------------------------------------ MANAGER


def test_every_configured_manager_gets_a_card_and_the_documents(two_managers, settings, ids):
    user = make_user(30)
    _to_final_review(two_managers, user, ids)
    conn = _conn(settings)
    try:
        merge_draft(conn, session_id_for(TEST_PROFILE.bot_key, 30), {"document_files": [
            {"file_id": "techpass", "file_unique_id": "u-tp", "kind": "photo", "mime_type": "image/jpeg", "file_size": 1}]})
    finally:
        conn.close()
    two_managers.press(user, "fc:continue")
    (order,) = _orders(settings, 30)
    calls = _receipt_photo(two_managers, user)
    for manager in (MANAGER, MANAGER_B):
        assert len(sent_to(calls, manager, SendMessage)) == 1
        photos = sent_to(calls, manager, SendPhoto)
        assert [p.photo for p in photos] == ["techpass", "rcpt-1"]
        assert "Документ ТС" in photos[0].caption
    conn = _conn(settings)
    try:
        assert {m.manager_user_id for m in bot_outbox.manager_messages(conn, order.id)} == {MANAGER, MANAGER_B}
    finally:
        conn.close()


def test_non_manager_cannot_act(harness, settings, ids):
    order, _ = _to_review(harness, make_user(31), ids)
    for action in ("confirm", "reject", "policy", "resend_policy"):
        calls = harness.press(make_user(555), f"mg:{action}:{order.id}")
        answer = [c for c in calls if isinstance(c, AnswerCallbackQuery)][0]
        assert answer.text == "Нет доступа"
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value


def test_customer_cannot_confirm_their_own_payment(harness, settings, ids):
    user = make_user(32)
    order, _ = _to_review(harness, user, ids)
    answer = [c for c in harness.press(user, f"mg:confirm:{order.id}") if isinstance(c, AnswerCallbackQuery)][0]
    assert answer.text == "Нет доступа"
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value


def test_order_of_another_bot_or_web_is_not_actionable(harness, settings, ids):
    order, _ = _to_review(harness, make_user(33), ids)
    conn = _conn(settings)
    try:
        conn.execute("UPDATE insurance_orders SET bot_key = 'otherbot' WHERE id = ?", (order.id,))
        conn.commit()
    finally:
        conn.close()
    answer = [c for c in harness.press(make_user(MANAGER), f"mg:confirm:{order.id}") if isinstance(c, AnswerCallbackQuery)][0]
    assert answer.text == "Заказ не найден"
    conn = _conn(settings)
    try:
        conn.execute("UPDATE insurance_orders SET bot_key = ?, channel = 'web' WHERE id = ?", (TEST_PROFILE.bot_key, order.id))
        conn.commit()
    finally:
        conn.close()
    answer = [c for c in harness.press(make_user(MANAGER), f"mg:confirm:{order.id}") if isinstance(c, AnswerCallbackQuery)][0]
    assert answer.text == "Заказ не найден"
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value


def test_malformed_callback_data_is_harmless(harness, settings, ids):
    order, _ = _to_review(harness, make_user(34), ids)
    for data in ("mg:confirm:abc", "mg:confirm", "mg::1", f"mg:confirm:{order.id}:extra"):
        harness.press(make_user(MANAGER), data)
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value


def test_repeated_confirm_is_already_processed(harness, settings, ids):
    order = _to_paid(harness, make_user(35), ids)
    answer = [c for c in harness.press(make_user(MANAGER), f"mg:confirm:{order.id}") if isinstance(c, AnswerCallbackQuery)][0]
    assert answer.text == "Уже обработано"
    assert _history(settings, order.id).count(("payment_review", "paid")) == 1


def test_manager_a_confirms_then_b_is_a_no_op_and_both_cards_update(two_managers, settings, ids):
    user = make_user(36)
    order, _ = _to_review(two_managers, user, ids)
    calls = two_managers.press(make_user(MANAGER), f"mg:confirm:{order.id}")
    assert _order(settings, order.id).status == OrderStatus.PAID.value
    edits = [c for c in calls if isinstance(c, EditMessageText)]
    assert {e.chat_id for e in edits} == {MANAGER, MANAGER_B}
    for edit in edits:
        assert "✅ Оплата подтверждена — оформите полис" in edit.text
        assert f"mg:confirm:{order.id}" not in [b[1] for b in buttons(edit)]
        # automatic tpl.ge issuance is off by default: the manual upload is the primary action
        assert buttons(edit)[0] == ("📄 Загрузить готовый полис", f"mg:policy:{order.id}")
        assert f"op:start:{order.id}" not in [b[1] for b in buttons(edit)]
    answer = [c for c in two_managers.press(make_user(MANAGER_B), f"mg:reject:{order.id}") if isinstance(c, AnswerCallbackQuery)][0]
    assert answer.text == "Уже обработано"
    assert _order(settings, order.id).status == OrderStatus.PAID.value


# ------------------------------------------------------------ CONFIRM/REJECT


def test_confirm_notifies_the_customer(harness, settings, ids):
    user = make_user(40)
    order, _ = _to_review(harness, user, ids)
    calls = harness.press(make_user(MANAGER), f"mg:confirm:{order.id}")
    (notice,) = sent_to(calls, 40, SendMessage)
    assert notice.text == (
        f"✅ Оплата подтверждена\n\nЗаказ {order.public_number} передан в оформление.\nМы пришлём готовый полис сюда."
    )
    assert _events(settings, "bot_payment_confirmed")[0]["via"] == "telegram"


def test_reject_then_new_receipt_starts_a_new_review_round(harness, settings, ids):
    user = make_user(41)
    order, _ = _to_review(harness, user, ids)
    calls = harness.press(make_user(MANAGER), f"mg:reject:{order.id}")
    assert _order(settings, order.id).status == OrderStatus.AWAITING_PAYMENT.value
    (notice,) = sent_to(calls, 41, SendMessage)
    assert notice.text.startswith("❌ Платёж пока не найден")
    assert FAKE_PHONE in notice.text and "Сумма: 2 149 ₽" in notice.text
    (card_edit,) = [c for c in calls if isinstance(c, EditMessageText) and c.chat_id == MANAGER]
    assert "❌ Оплата не найдена — ждём новый чек" in card_edit.text
    assert buttons(card_edit) == []

    calls = _receipt_photo(harness, user, file_id="rcpt-2")
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value
    assert len(sent_to(calls, MANAGER, SendMessage)) == 1  # a fresh card for the new round
    assert [p.photo for p in sent_to(calls, MANAGER, SendPhoto)] == ["rcpt-1", "rcpt-2"]
    assert len(_files(settings, order.id, order_files.KIND_PAYMENT_RECEIPT)) == 2  # old receipt kept


# -------------------------------------------------------------------- POLICY


def test_policy_upload_and_delivery(harness, settings, ids):
    user = make_user(50)
    order = _to_paid(harness, user, ids)
    prompt = sent_to(harness.press(make_user(MANAGER), f"mg:policy:{order.id}"), MANAGER, SendMessage)[0]
    assert prompt.text.startswith(f"📄 Отправьте PDF полиса по заказу {order.public_number}")
    calls = _policy_pdf(harness, MANAGER)
    (delivered,) = sent_to(calls, 50, SendDocument)
    assert delivered.document == "policy-1"
    assert delivered.caption == (
        f"🎉 Страховка готова\n\nПолис по заказу {order.public_number} приложен к сообщению.\nСохраните файл на время поездки."
    )
    assert _order(settings, order.id).status == OrderStatus.POLICY_READY.value
    assert _history(settings, order.id)[-2:] == [("paid", "processing"), ("processing", "policy_ready")]
    assert [f.telegram_file_id for f in _files(settings, order.id, order_files.KIND_POLICY)] == ["policy-1"]
    assert any("Полис по заказу" in c.text and "отправлен клиенту" in c.text for c in sent_to(calls, MANAGER, SendMessage))
    assert any("📄 Полис отправлен" in c.text for c in calls if isinstance(c, EditMessageText))
    assert _events(settings, "bot_policy_delivered")[0]["order_id"] == order.id
    conn = _conn(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM telegram_manager_upload_contexts").fetchone()[0] == 0
    finally:
        conn.close()


def test_policy_goes_only_to_the_orders_own_customer(harness, settings, ids):
    alice, bob = make_user(51), make_user(52)
    order_a = _to_paid(harness, alice, ids)
    order_b = _to_paid(harness, bob, ids)
    harness.press(make_user(MANAGER), f"mg:policy:{order_a.id}")
    calls = _policy_pdf(harness, MANAGER, file_id="policy-for-a")
    assert [c.document for c in sent_to(calls, 51, SendDocument)] == ["policy-for-a"]
    assert sent_to(calls, 52) == []
    assert _files(settings, order_b.id, order_files.KIND_POLICY) == []
    # the latest "📄 Отправить полис" tap decides the target -- re-validated from the DB
    harness.press(make_user(MANAGER), f"mg:policy:{order_b.id}")
    calls = _policy_pdf(harness, MANAGER, file_id="policy-for-b")
    assert [c.document for c in sent_to(calls, 52, SendDocument)] == ["policy-for-b"]
    assert sent_to(calls, 51) == []


def test_non_manager_pdf_is_never_a_policy(harness, settings, ids):
    order = _to_paid(harness, make_user(53), ids)
    harness.press(make_user(MANAGER), f"mg:policy:{order.id}")
    calls = harness.send_document(make_user(777), b"%PDF", file_id="evil", mime_type="application/pdf", file_name="p.pdf")
    assert _files(settings, order.id, order_files.KIND_POLICY) == []
    assert sent_to(calls, 53) == []


def test_manager_pdf_without_active_context_is_not_a_policy(harness, settings, ids):
    order = _to_paid(harness, make_user(54), ids)
    calls = _policy_pdf(harness, MANAGER)
    assert _files(settings, order.id, order_files.KIND_POLICY) == []
    assert sent_to(calls, 54) == []


def test_non_pdf_is_rejected_and_context_kept(harness, settings, ids):
    order = _to_paid(harness, make_user(55), ids)
    harness.press(make_user(MANAGER), f"mg:policy:{order.id}")
    screen = last_screen(_policy_pdf(harness, MANAGER, file_id="img", mime="image/jpeg", name="policy.jpg"))
    assert "Нужен PDF-файл" in screen.text
    renamed = last_screen(_policy_pdf(harness, MANAGER, file_id="fake", mime="application/pdf", name="policy.exe"))
    assert "Нужен PDF-файл" in renamed.text
    assert _files(settings, order.id, order_files.KIND_POLICY) == []
    _policy_pdf(harness, MANAGER)  # the context is still active
    assert _order(settings, order.id).status == OrderStatus.POLICY_READY.value


def test_policy_not_allowed_before_payment_confirmed(harness, settings, ids):
    order, _ = _to_review(harness, make_user(56), ids)
    answer = [c for c in harness.press(make_user(MANAGER), f"mg:policy:{order.id}") if isinstance(c, AnswerCallbackQuery)][0]
    assert answer.text == "Уже обработано"
    _policy_pdf(harness, MANAGER)
    assert _files(settings, order.id, order_files.KIND_POLICY) == []


def test_replayed_policy_upload_is_not_delivered_twice(harness, settings, ids):
    user = make_user(57)
    order = _to_paid(harness, user, ids)
    harness.press(make_user(MANAGER), f"mg:policy:{order.id}")
    _policy_pdf(harness, MANAGER)
    calls = _policy_pdf(harness, MANAGER)  # same file again, context already consumed
    assert sent_to(calls, 57, SendDocument) == []
    answer = [c for c in harness.press(make_user(MANAGER), f"mg:policy:{order.id}") if isinstance(c, AnswerCallbackQuery)][0]
    assert answer.text == "Уже обработано"


def test_failed_policy_delivery_can_be_retried(harness, settings, ids):
    user = make_user(58)
    order = _to_paid(harness, user, ids)
    harness.press(make_user(MANAGER), f"mg:policy:{order.id}")
    harness.session.fail_chats.add(58)
    calls = _policy_pdf(harness, MANAGER)
    assert _order(settings, order.id).status == OrderStatus.PROCESSING.value  # not POLICY_READY
    assert len(_files(settings, order.id, order_files.KIND_POLICY)) == 1  # kept
    failure = [c for c in sent_to(calls, MANAGER, SendMessage) if "Не удалось отправить полис" in c.text][0]
    assert (f"🔁 Отправить полис повторно", f"mg:resend_policy:{order.id}") in buttons(failure)
    harness.session.fail_chats.clear()
    calls = harness.press(make_user(MANAGER), f"mg:resend_policy:{order.id}")
    assert [c.document for c in sent_to(calls, 58, SendDocument)] == ["policy-1"]
    assert _order(settings, order.id).status == OrderStatus.POLICY_READY.value


def test_expired_upload_context(harness, settings, ids):
    order = _to_paid(harness, make_user(59), ids)
    harness.press(make_user(MANAGER), f"mg:policy:{order.id}")
    conn = _conn(settings)
    try:
        conn.execute("UPDATE telegram_manager_upload_contexts SET expires_at = '2000-01-01T00:00:00+00:00'")
        conn.commit()
    finally:
        conn.close()
    assert "истекло" in last_screen(_policy_pdf(harness, MANAGER)).text
    assert _files(settings, order.id, order_files.KIND_POLICY) == []


def test_upload_context_survives_restart(settings, ids):
    user = make_user(60)
    first = BotHarness(settings, manager_ids={MANAGER})
    order = _to_paid(first, user, ids)
    first.press(make_user(MANAGER), f"mg:policy:{order.id}")
    first.close()
    restarted = BotHarness(settings, manager_ids={MANAGER})
    try:
        calls = _policy_pdf(restarted, MANAGER)
        assert sent_to(calls, 60, SendDocument)
    finally:
        restarted.close()
    assert _order(settings, order.id).status == OrderStatus.POLICY_READY.value


def test_cancel_upload(harness, settings, ids):
    order = _to_paid(harness, make_user(61), ids)
    harness.press(make_user(MANAGER), f"mg:policy:{order.id}")
    harness.press(make_user(MANAGER), f"mg:cancel_upload:{order.id}")
    _policy_pdf(harness, MANAGER)
    assert _files(settings, order.id, order_files.KIND_POLICY) == []


# -------------------------------------------------------------------- RESUME


def test_start_resumes_each_order_state(harness, settings, ids):
    user = make_user(70)
    order, _ = _place_order(harness, user, ids)
    # unfinished: /start OFFERS the order (never a forced resume) ...
    offer = last_screen(harness.send_text(user, "/start"))
    assert offer.text.startswith(f"У вас есть незавершённый заказ {order.public_number}")
    assert ("↩️ Вернуться к заказу", f"o:resume:{order.id}") in buttons(offer)
    # ... and "Вернуться к заказу" is exactly the old resume
    assert last_screen(harness.press(user, f"o:resume:{order.id}")).text.startswith("💳 Оплата заказа")
    _receipt_photo(harness, user)
    harness.send_text(user, "/start")
    assert last_screen(harness.press(user, f"o:resume:{order.id}")).text.startswith("🧾 Чек по заказу")
    harness.press(make_user(MANAGER), f"mg:confirm:{order.id}")
    paid = last_screen(harness.send_text(user, "/start"))
    assert paid.text.startswith("✅ Оплата по заказу") and ("➕ Оформить ещё одну страховку", f"o:new:{order.id}") in buttons(paid)
    harness.press(make_user(MANAGER), f"mg:policy:{order.id}")
    _policy_pdf(harness, MANAGER)
    ready = last_screen(harness.send_text(user, "/start"))
    assert ready.text.startswith("🎉 Полис по заказу")
    assert [t for t, _ in buttons(ready)] == ["📄 Прислать полис ещё раз", "➕ Оформить ещё одну страховку"]
    resent = sent_to(harness.press(user, f"o:resend_policy:{order.id}"), 70, SendDocument)
    assert [c.document for c in resent] == ["policy-1"]


def test_resume_survives_restart_in_awaiting_and_review(settings, ids):
    user = make_user(71)
    first = BotHarness(settings, manager_ids={MANAGER})
    _place_order(first, user, ids)
    first.close()
    second = BotHarness(settings, manager_ids={MANAGER})
    order = _orders(settings, 71)[0]
    second.send_text(user, "/start")
    assert last_screen(second.press(user, f"o:resume:{order.id}")).text.startswith("💳 Оплата заказа")
    _receipt_photo(second, user)
    second.close()
    third = BotHarness(settings, manager_ids={MANAGER})
    try:
        assert last_screen(third.send_text(user, "привет")).text.startswith("Пожалуйста, воспользуйтесь кнопками ниже.\n\n🧾 Чек")
    finally:
        third.close()


def test_new_purchase_only_after_payment(harness, settings, ids):
    user = make_user(72)
    order, _ = _place_order(harness, user, ids, source="upper_lars")
    calls = harness.press(user, f"o:new:{order.id}")
    alert = [c for c in calls if isinstance(c, AnswerCallbackQuery)][0]
    assert alert.show_alert and "Сначала завершите оплату" in alert.text
    assert _draft(settings, 72)["order_id"] == order.id
    # old checkout buttons can't start a second unpaid order either
    assert last_screen(harness.press(user, "p:passenger_car:90d")).text.startswith("💳 Оплата заказа")
    assert len(_orders(settings, 72)) == 1

    _receipt_photo(harness, user)
    harness.press(make_user(MANAGER), f"mg:confirm:{order.id}")
    fresh = last_screen(harness.press(user, f"o:new:{order.id}"))
    assert fresh.text == "Выберите тип транспортного средства:"
    draft = _draft(settings, 72)
    assert "order_id" not in draft and draft["acquisition_source"] == "upper_lars"
    assert _order(settings, order.id).status == OrderStatus.PAID.value  # old order untouched


def test_checkout_buttons_after_order_show_the_order(harness, settings, ids):
    user = make_user(73)
    _place_order(harness, user, ids)
    for data in ("m:apply", "d:tomorrow", "e:manual", "n:restart_confirmed:", "n:categories:"):
        screen = last_screen(harness.press(user, data))
        assert screen.text.startswith("💳 Оплата заказа"), data
    assert len(_orders(settings, 73)) == 1


def test_someone_elses_order_id_in_customer_buttons_is_ignored(harness, settings, ids):
    order_a, _ = _place_order(harness, make_user(74), ids)
    _receipt_photo(harness, make_user(74))
    harness.press(make_user(MANAGER), f"mg:confirm:{order_a.id}")
    harness.press(make_user(MANAGER), f"mg:policy:{order_a.id}")
    _policy_pdf(harness, MANAGER)
    stranger = make_user(75)
    calls = harness.press(stranger, f"o:resend_policy:{order_a.id}")
    assert sent_to(calls, 75, SendDocument) == [] and sent_to(calls, 74) == []


# ---------------------------------------------------------- FAILURE RECOVERY


def test_manager_notification_failure_is_retried_without_duplicates(two_managers, settings, ids):
    user = make_user(80)
    order, _ = _place_order(two_managers, user, ids)
    two_managers.session.fail_chats.add(MANAGER_B)
    calls = _receipt_photo(two_managers, user)
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value  # never rolled back
    assert len(_files(settings, order.id, order_files.KIND_PAYMENT_RECEIPT)) == 1  # never lost
    assert len(sent_to(calls, MANAGER, SendMessage)) == 1
    two_managers.session.fail_chats.clear()
    make_outbox_due(settings.app.db_file)
    worker = two_managers.dispatcher["outbox"]
    before = len(two_managers.session.calls)
    conn = _conn(settings)
    try:
        two_managers.loop.run_until_complete(worker.process(two_managers.bot, conn))
    finally:
        conn.close()
    retried = two_managers.session.calls[before:]
    assert len(sent_to(retried, MANAGER_B, SendMessage)) == 1
    assert [p.photo for p in sent_to(retried, MANAGER_B, SendPhoto)] == ["rcpt-1"]
    assert sent_to(retried, MANAGER) == []  # manager A is not spammed again


def test_customer_notice_failure_is_retried(harness, settings, ids):
    user = make_user(81)
    order, _ = _to_review(harness, user, ids)
    harness.session.fail_chats.add(81)
    harness.press(make_user(MANAGER), f"mg:confirm:{order.id}")
    assert _order(settings, order.id).status == OrderStatus.PAID.value
    harness.session.fail_chats.clear()
    make_outbox_due(settings.app.db_file)
    before = len(harness.session.calls)
    conn = _conn(settings)
    try:
        harness.loop.run_until_complete(harness.dispatcher["outbox"].process(harness.bot, conn))
    finally:
        conn.close()
    assert [c.text.split("\n")[0] for c in sent_to(harness.session.calls[before:], 81, SendMessage)] == ["✅ Оплата подтверждена"]


# ------------------------------------------------------------------- PRIVACY


def test_no_personal_data_payment_details_or_token_in_logs_or_events(harness, settings, ids, caplog):
    with caplog.at_level(logging.DEBUG):
        user = make_user(90, "carol")
        order = _to_paid(harness, user, ids)
        harness.press(make_user(MANAGER), f"mg:policy:{order.id}")
        _policy_pdf(harness, MANAGER)
    dumped = _all_event_json(settings)
    for secret in (VIN, "AB1234567", "Ivanov", "ivan@example.com", "79001234567", "rcpt-1", "policy-1", FAKE_PHONE, FAKE_RECIPIENT):
        assert secret not in dumped
        assert secret not in caplog.text
    assert FAKE_TOKEN not in caplog.text
