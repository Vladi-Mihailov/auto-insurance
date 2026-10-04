"""Managers and the owner inside the SAME bot customers use.

Roles live in telegram_bot_staff (bootstrapped from TELEGRAM_BOT_MANAGER_IDS
/ TELEGRAM_BOT_OWNER_ID; more managers join through an owner's one-time
invite link). Every staff screen and action re-checks the role server-side;
confirm/reject from a list card goes through the SAME handler and payment
service as from the receipt notification.

All offline: fake Bot API session, tmp DB, no OCR (manual entry)."""

import asyncio
import dataclasses
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update

from app.countries import COUNTRIES
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.orders import files as order_files
from app.orders.repository import get_order_by_id, list_telegram_orders
from app.orders.state_machine import OrderStatus
from app.settings import load_settings
from app.telegram_bot import staff, texts
from app.telegram_bot.config import BotConfigError, parse_owner_id
from app.telegram_bot.sessions import session_id_for
from telegram_bot_helpers import (
    TEST_PROFILE,
    BotHarness,
    buttons,
    last_screen,
    make_outbox_due,
    make_user,
    screens,
    seed_catalog,
    sent_to,
)

VIN = "WVWZZZ1JZXW000001"
OWNER = 999  # the sole configured manager -> bootstrapped as owner
PROFILE = dataclasses.replace(
    TEST_PROFILE, username="OsagoTestBot", customer_email="tplgee@mail.ru", customer_phone="+995 574 22 06 25"
)
CUSTOMER_MENU = ["🚗 Оформить страховку"]
MANAGER_MENU = ["🚗 Оформить страховку", "📋 Заказы", "⏳ Ожидают оплаты", "💰 Цены"]
OWNER_MENU = [*MANAGER_MENU, "👥 Менеджеры"]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", "+79495205223")
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", "Владимир М.")
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


@pytest.fixture
def h(settings, ids):
    harness = BotHarness(settings, profile=PROFILE, manager_ids={OWNER})
    yield harness
    harness.close()


# ------------------------------------------------------------------ helpers


def _conn(settings):
    return get_connection(settings.app.db_file)


def _role(settings, user_id):
    conn = _conn(settings)
    try:
        return staff.role_of(conn, PROFILE.bot_key, user_id)
    finally:
        conn.close()


def _order(settings, order_id):
    conn = _conn(settings)
    try:
        return get_order_by_id(conn, order_id)
    finally:
        conn.close()


def _orders(settings, user_id):
    conn = _conn(settings)
    try:
        return list_telegram_orders(conn, bot_key=PROFILE.bot_key, telegram_user_id=user_id)
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


def _invite_count(settings):
    conn = _conn(settings)
    try:
        return conn.execute("SELECT COUNT(*) FROM telegram_bot_staff_invites").fetchone()[0]
    finally:
        conn.close()


def _menu(h, user):
    return [t for t, _ in buttons(last_screen(h.send_text(user, "/start")))]


def _name(user_id: int) -> str:
    """A valid policyholder name (letters only) unique per test user."""
    return "Client " + "".join(chr(ord("A") + int(d)) for d in str(user_id))


def _place_order(h, user, ids, plate="AB123CD"):
    for step in (
        lambda: h.send_text(user, "/start"),
        lambda: h.press(user, "p:passenger_car:30d"),
        lambda: h.press(user, "d:tomorrow"),
        lambda: h.press(user, "e:manual"),
        lambda: h.send_text(user, plate),
        lambda: h.send_text(user, VIN),
        lambda: h.press(user, f"mf:{ids['TOYOTA']}"),
        lambda: h.press(user, f"md:{ids['TOYOTA/CAMRY']}"),
        lambda: h.press(user, "vc"),
        lambda: h.send_text(user, _name(user.id)),
        lambda: h.send_text(user, "AB1234567"),
        lambda: h.press(user, f"cz:{COUNTRIES.index('Russia')}"),
    ):
        step()
    h.press(user, "fc:confirm")
    return _orders(h.dispatcher["settings"], user.id)[0]


def _to_review(h, user, ids, file_id=None):
    order = _place_order(h, user, ids)
    calls = h.send_photo(user, b"receipt", file_id=file_id or f"rcpt-{user.id}")
    return order, calls


def _invite_link(h, owner=OWNER) -> str:
    screen = last_screen(h.press(make_user(owner), "st:invite:0:0"))
    (link,) = re.findall(r"https://t\.me/OsagoTestBot\?start=mgr_[A-Za-z0-9_-]+", screen.text)
    return link


def _token(link: str) -> str:
    return link.split("start=", 1)[1]


def _add_manager(h, user_id, username=None):
    h.send_text(make_user(user_id, username), f"/start {_token(_invite_link(h))}")
    return make_user(user_id, username)


def _alerts(calls):
    return [c for c in calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]


def _press_at_once(h, presses):
    """Feed several callback presses concurrently (e.g. two managers)."""
    def update(n, user, data):
        message = Message(message_id=50_000 + n, date=datetime.now(timezone.utc), chat=Chat(id=user.id, type="private"), text="…")
        return Update(update_id=90_000 + n, callback_query=CallbackQuery(id=f"race-{n}", from_user=user, chat_instance="ci", data=data, message=message))

    async def run():
        await asyncio.gather(*(h.dispatcher.feed_update(h.bot, update(n, u, d)) for n, (u, d) in enumerate(presses)))

    start = len(h.session.calls)
    h.loop.run_until_complete(run())
    return h.session.calls[start:]


# ----------------------------------------------------------------- CUSTOMER


def test_customer_sees_the_normal_menu_only(h, settings, ids):
    screen = last_screen(h.send_text(make_user(10), "/start"))
    assert [t for t, _ in buttons(screen)] == CUSTOMER_MENU
    assert not any(d.startswith("st:") for _, d in buttons(screen))


def test_customer_cannot_invoke_any_staff_callback(h, settings, ids):
    victim = make_user(11)
    order, _ = _to_review(h, victim, ids)
    attacker = make_user(12)
    h.send_text(attacker, "/start")
    for data in (
        "st:orders:0:0", "st:pending:0:0", f"st:cp:{order.id}:0", f"st:co:{order.id}:0", f"st:receipt:{order.id}:0",
        "st:menu:0:0", "st:managers:0:0", "st:invite:0:0", "st:rmlist:0:0", f"st:rm:{OWNER}:0",
        f"mg:confirm:{order.id}", f"mg:reject:{order.id}", f"mg:lconfirm:{order.id}", f"mg:policy:{order.id}",
    ):
        calls = h.press(attacker, data)
        (alert,) = _alerts(calls)
        assert alert.text == texts.MGR_NO_ACCESS, data
        # nothing about the order (or anyone) reaches the attacker
        assert not screens(calls) and not sent_to(calls, 12, SendPhoto) and not sent_to(calls, 12, SendDocument), data
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value
    assert _invite_count(settings) == 0 and _role(settings, OWNER) == "owner"


def test_plain_or_source_start_never_grants_staff(h, settings, ids):
    for payload in ("/start", "/start upper_lars", "/start mgr_", "/start mgr_short", f"/start mgr_{'x' * 32}"):
        h.send_text(make_user(13), payload)
        assert _role(settings, 13) is None, payload
    assert _menu(h, make_user(13)) == CUSTOMER_MENU


# ------------------------------------------------------------------ MANAGER


def test_manager_and_owner_menus(h, settings, ids):
    manager = _add_manager(h, 20, "mgr20")
    assert _menu(h, manager) == MANAGER_MENU
    assert _menu(h, make_user(OWNER)) == OWNER_MENU


def test_manager_can_still_buy_insurance(h, settings, ids):
    manager = _add_manager(h, 21)
    order = _place_order(h, manager, ids)
    assert order.telegram_user_id == 21 and order.status == OrderStatus.AWAITING_PAYMENT.value
    # the menu still offers the manager functions next to their own order
    assert {"📋 Заказы", "⏳ Ожидают оплаты"} <= set(_menu(h, manager))


def test_pending_list_and_card(h, settings, ids):
    manager = _add_manager(h, 22)
    in_review, _ = _to_review(h, make_user(30), ids)
    awaiting = _place_order(h, make_user(31), ids)  # no receipt yet -> not pending
    screen = last_screen(h.press(manager, "st:pending:0:0"))
    assert screen.text.startswith("⏳ Ожидают проверки оплаты: 1")
    items = [(t, d) for t, d in buttons(screen) if d.startswith("st:cp:")]
    assert items == [(f"{in_review.public_number} · {_name(30)} · 2 149 ₽", f"st:cp:{in_review.id}:0")]
    assert not any(str(awaiting.id) in d.split(":")[2] for _, d in items)

    card = last_screen(h.press(manager, f"st:cp:{in_review.id}:0"))
    for fragment in (
        in_review.public_number, "Telegram ID 30", f"ФИО {_name(30)}", "Госномер AB123CD", "Марка TOYOTA",
        "Сумма к получению: 2 149 ₽", "Получатель: Владимир М.", "Сбербанк · +7 949 520-52-23", "Чек: получен (1)",
        "⏳ Ожидает проверки оплаты",
    ):
        assert fragment in card.text, fragment
    assert buttons(card)[:2] == [
        ("✅ Оплата поступила", f"mg:lconfirm:{in_review.id}"), ("❌ Оплата не поступила", f"mg:lreject:{in_review.id}"),
    ]
    assert ("🧾 Показать чек", f"st:receipt:{in_review.id}:0") in buttons(card)
    assert ("⬅️ К ожидающим оплаты", "st:pending:0:0") in buttons(card)


def test_receipt_view_sends_exactly_that_orders_receipt(h, settings, ids):
    manager = _add_manager(h, 23)
    first, _ = _to_review(h, make_user(32), ids, file_id="rcpt-first")
    _to_review(h, make_user(33), ids, file_id="rcpt-second")
    calls = h.press(manager, f"st:receipt:{first.id}:0")
    photos = sent_to(calls, 23, SendPhoto)
    assert [p.photo for p in photos] == ["rcpt-first"]
    assert photos[0].caption == f"🧾 Чек об оплате · {first.public_number}\nОжидается: 2 149 ₽ · {_name(32)}"
    # an order without a receipt: a short alert, nothing sent
    no_receipt = _place_order(h, make_user(34), ids)
    calls = h.press(manager, f"st:receipt:{no_receipt.id}:0")
    assert _alerts(calls)[0].text == texts.STAFF_NO_RECEIPT and not sent_to(calls, 23, SendPhoto)


def test_recent_orders_with_status_labels_and_pages(h, settings, ids):
    manager = _add_manager(h, 24)
    orders = [_place_order(h, make_user(40 + n), ids) for n in range(9)]
    paid, rejected, review = orders[0], orders[1], orders[2]
    for o in (paid, rejected, review):
        h.send_photo(make_user(o.telegram_user_id), b"r", file_id=f"r-{o.id}")
    h.press(make_user(OWNER), f"mg:confirm:{paid.id}")
    h.press(make_user(OWNER), f"mg:reject:{rejected.id}")

    page1 = last_screen(h.press(manager, "st:orders:0:0"))
    labels = {d: t for t, d in buttons(page1)}
    assert len([d for d in labels if d.startswith("st:co:")]) == 8  # paged, never the whole history
    assert ("▶️", "st:orders:0:1") in buttons(page1)
    page2 = last_screen(h.press(manager, "st:orders:0:1"))
    items2 = {d: t for t, d in buttons(page2) if d.startswith("st:co:")}
    assert len(items2) == 1 and ("◀️", "st:orders:0:0") in buttons(page2)
    every = {**labels, **items2}
    label_of = {int(d.split(":")[2]): t for d, t in every.items() if d.startswith("st:co:")}
    assert len(label_of) == 9
    assert label_of[paid.id] == f"{paid.public_number} · ✅ Оплачено"
    assert label_of[rejected.id] == f"{rejected.public_number} · ❌ Оплата отклонена"
    assert label_of[review.id] == f"{review.public_number} · ⏳ Проверка оплаты"
    assert label_of[orders[8].id] == f"{orders[8].public_number} · 💳 Ждём оплату"
    card = last_screen(h.press(manager, f"st:co:{paid.id}:1"))
    assert "✅ Оплата подтверждена — оформите полис" in card.text and ("⬅️ К заказам", "st:orders:0:1") in buttons(card)


def test_confirm_from_the_list_is_the_same_payment_flow(h, settings, ids):
    manager = _add_manager(h, 25)
    order, receipt_calls = _to_review(h, make_user(35), ids)
    calls = h.press(manager, f"mg:lconfirm:{order.id}")
    assert _order(settings, order.id).status == OrderStatus.PAID.value
    assert [c.text.split("\n")[0] for c in sent_to(calls, 35, SendMessage)] == ["✅ Оплата подтверждена"]
    pressed = [c for c in calls if isinstance(c, EditMessageText) and c.chat_id == 25]
    assert any([t for t, _ in buttons(c)][0] == "📄 Загрузить готовый полис" for c in pressed)  # the list card, refreshed
    # the notification cards (owner + manager) were updated by the outbox as usual
    assert any(isinstance(c, EditMessageText) and c.chat_id == OWNER for c in calls)
    # ... and the existing policy flow follows
    h.press(manager, f"mg:policy:{order.id}")
    delivered = h.send_document(manager, b"%PDF-1.4", file_id="pol-1", mime_type="application/pdf", file_name="p.pdf")
    assert [d.document for d in sent_to(delivered, 35, SendDocument)] == ["pol-1"]


def test_reject_from_the_list_is_the_same_rejection_flow(h, settings, ids):
    manager = _add_manager(h, 26)
    order, _ = _to_review(h, make_user(36), ids)
    calls = h.press(manager, f"mg:lreject:{order.id}")
    assert _order(settings, order.id).status == OrderStatus.AWAITING_PAYMENT.value
    (notice,) = sent_to(calls, 36, SendMessage)
    assert notice.text.startswith("❌ Платёж пока не найден") and "+7 949 520-52-23" in notice.text
    # resubmission starts a new round that shows up in ⏳ again
    h.send_photo(make_user(36), b"r2", file_id="rcpt-36-b")
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value
    assert f"st:cp:{order.id}:0" in [d for _, d in buttons(last_screen(h.press(manager, "st:pending:0:0")))]


# ------------------------------------------------------- MULTIPLE MANAGERS


def test_receipt_notification_reaches_every_active_manager_exactly_once(h, settings, ids):
    for manager_id in (50, 51, 52):
        _add_manager(h, manager_id)
    h.press(make_user(OWNER), "st:rm:52:0")
    order, calls = _to_review(h, make_user(37), ids)
    for manager_id in (OWNER, 50, 51):
        assert len(sent_to(calls, manager_id, SendMessage)) == 1, manager_id  # one card each
        assert [p.photo for p in sent_to(calls, manager_id, SendPhoto)] == ["rcpt-37"]
    assert sent_to(calls, 52) == []  # a removed manager gets nothing
    extra = h.send_photo(make_user(37), b"r2", file_id="rcpt-37-b")
    for manager_id in (OWNER, 50, 51):
        assert [p.photo for p in sent_to(extra, manager_id, SendPhoto)] == ["rcpt-37-b"]
    assert sent_to(extra, 52) == []


def test_two_managers_confirming_pay_only_once(h, settings, ids):
    a = _add_manager(h, 53)
    order, _ = _to_review(h, make_user(38), ids)
    first = h.press(a, f"mg:lconfirm:{order.id}")  # from the list
    second = h.press(make_user(OWNER), f"mg:confirm:{order.id}")  # the old notification button
    assert _alerts(second)[0].text == texts.MGR_ALREADY_DONE
    assert _history(settings, order.id).count(("payment_review", "paid")) == 1
    notices = sent_to(first + second, 38, SendMessage)
    assert [n.text.split("\n")[0] for n in notices] == ["✅ Оплата подтверждена"]  # one customer notice
    # the stale card is re-rendered with the real state (no confirm buttons)
    refreshed = [c for c in second if isinstance(c, EditMessageText) and c.chat_id == OWNER]
    assert refreshed and not any(d.startswith("mg:confirm") for _, d in buttons(refreshed[-1]))


def test_simultaneous_confirm_and_reject_resolve_to_one_decision(h, settings, ids):
    a = _add_manager(h, 54)
    order, _ = _to_review(h, make_user(39), ids)
    calls = _press_at_once(h, [(a, f"mg:confirm:{order.id}"), (make_user(OWNER), f"mg:reject:{order.id}")])
    decisions = [t for t in _history(settings, order.id) if t[0] == "payment_review"]
    assert len(decisions) == 1
    assert len(sent_to(calls, 39, SendMessage)) == 1  # exactly one customer notice
    assert len([c for c in _alerts(calls) if c.text == texts.MGR_ALREADY_DONE]) == 1


def test_simultaneous_double_confirm_never_issues_twice(h, settings, ids):
    a = _add_manager(h, 55)
    order, _ = _to_review(h, make_user(56), ids)
    calls = _press_at_once(h, [(a, f"mg:confirm:{order.id}"), (make_user(OWNER), f"mg:lconfirm:{order.id}")])
    assert _history(settings, order.id).count(("payment_review", "paid")) == 1
    assert len(sent_to(calls, 56, SendMessage)) == 1
    assert sent_to(calls, 56, SendDocument) == []  # no policy goes out because of a click


def test_queued_notification_for_a_removed_manager_is_never_delivered(h, settings, ids):
    _add_manager(h, 57)
    h.session.fail_chats.add(57)  # delivery to 57 fails -> stays queued
    _to_review(h, make_user(58), ids)
    h.session.fail_chats.clear()
    h.press(make_user(OWNER), "st:rm:57:0")
    make_outbox_due(settings.app.db_file)
    before = len(h.session.calls)
    conn = _conn(settings)
    try:
        h.loop.run_until_complete(h.dispatcher["outbox"].process(h.bot, conn))
    finally:
        conn.close()
    assert sent_to(h.session.calls[before:], 57) == []


# --------------------------------------------------------------------- OWNER


def test_owner_screen_lists_staff_and_managers_cannot_open_it(h, settings, ids):
    _add_manager(h, 60, "anna")
    _add_manager(h, 61)
    screen = last_screen(h.press(make_user(OWNER), "st:managers:0:0"))
    assert screen.text == "👥 Менеджеры\n\n👑 ID 999\n👤 @anna\n👤 ID 61"
    assert [t for t, _ in buttons(screen)] == ["➕ Добавить менеджера", "➖ Удалить менеджера", "⬅️ Назад"]
    for data in ("st:managers:0:0", "st:invite:0:0", "st:rmlist:0:0", "st:rmask:61:0", "st:rm:61:0", f"st:rm:{OWNER}:0"):
        calls = h.press(make_user(60), data)
        assert _alerts(calls)[0].text == texts.STAFF_OWNER_ONLY, data
        assert not screens(calls)
    assert _role(settings, 61) == "manager" and _role(settings, OWNER) == "owner"
    assert _invite_count(settings) == 2  # only the owner's two invites


def test_invite_binds_to_the_account_that_opens_it(h, settings, ids, caplog):
    with caplog.at_level(logging.INFO):
        screen = last_screen(h.press(make_user(OWNER), "st:invite:0:0"))
    (link,) = re.findall(r"https://t\.me/OsagoTestBot\?start=mgr_[A-Za-z0-9_-]+", screen.text)
    assert "одноразовая и действует 24 часа" in screen.text
    token = _token(link)
    assert token.startswith("mgr_") and len(token) > 30
    assert token not in caplog.text and token[4:] not in caplog.text  # never logged

    newbie = make_user(70, "newbie")
    calls = h.send_text(newbie, f"/start {token}")
    welcome = last_screen(calls)
    assert welcome.text.startswith(texts.STAFF_INVITE_ADDED)
    assert [t for t, _ in buttons(welcome)] == MANAGER_MENU
    assert _role(settings, 70) == "manager"  # never owner
    assert [m.text for m in sent_to(calls, OWNER, SendMessage)] == ["✅ Новый менеджер: @newbie"]
    conn = _conn(settings)
    try:
        member = staff.get_member(conn, PROFILE.bot_key, 70)
        draft_source = conn.execute(
            "SELECT draft_data FROM insurance_sessions WHERE session_id = ?", (session_id_for(PROFILE.bot_key, 70),)
        ).fetchone()[0]
    finally:
        conn.close()
    assert (member.username, member.source) == ("newbie", "invite")
    assert "mgr_" not in (draft_source or "")  # the invite is never stored as an acquisition source


def test_invite_is_single_use(h, settings, ids):
    token = _token(_invite_link(h))
    h.send_text(make_user(71), f"/start {token}")
    calls = h.send_text(make_user(72), f"/start {token}")
    assert last_screen(calls).text.startswith(texts.STAFF_INVITE_INVALID)
    assert _role(settings, 72) is None and _role(settings, 71) == "manager"
    # the same account reusing it changes nothing either
    assert last_screen(h.send_text(make_user(71), f"/start {token}")).text.startswith(texts.STAFF_INVITE_ALREADY)


def test_expired_invite_fails(h, settings, ids, monkeypatch):
    token = _token(_invite_link(h))
    later = datetime.now(timezone.utc) + staff.INVITE_TTL + timedelta(minutes=1)
    monkeypatch.setattr(staff, "_now", lambda: later)
    assert last_screen(h.send_text(make_user(73), f"/start {token}")).text.startswith(texts.STAFF_INVITE_INVALID)
    assert _role(settings, 73) is None


def test_invalid_invite_tokens_fail(h, settings, ids):
    real = _token(_invite_link(h))
    for payload in (f"mgr_{'A' * 32}", real[:-1] + ("x" if real[-1] != "x" else "y"), "mgr_../../etc", "mgr_" + "a" * 61):
        calls = h.send_text(make_user(74), f"/start {payload}")
        assert last_screen(calls).text.startswith(texts.STAFF_INVITE_INVALID), payload
    assert _role(settings, 74) is None
    # the real link was not used up by the failed attempts
    h.send_text(make_user(75), f"/start {real}")
    assert _role(settings, 75) == "manager"


def test_an_existing_manager_opening_an_invite_keeps_it_for_the_invitee(h, settings, ids):
    manager = _add_manager(h, 76)
    token = _token(_invite_link(h))
    assert last_screen(h.send_text(manager, f"/start {token}")).text.startswith(texts.STAFF_INVITE_ALREADY)
    assert _role(settings, 76) == "manager"  # never promoted by an invite
    h.send_text(make_user(77), f"/start {token}")
    assert _role(settings, 77) == "manager"


def test_manager_removal_takes_effect_immediately(h, settings, ids):
    manager = _add_manager(h, 80, "leaving")
    order, _ = _to_review(h, make_user(81), ids)
    owner = make_user(OWNER)
    pick = last_screen(h.press(owner, "st:rmlist:0:0"))
    assert buttons(pick) == [("➖ @leaving", "st:rmask:80:0"), ("⬅️ К менеджерам", "st:managers:0:0")]
    ask = last_screen(h.press(owner, "st:rmask:80:0"))
    assert ask.text.startswith("Удалить @leaving из менеджеров?")
    assert _role(settings, 80) == "manager"  # nothing happens before the confirmation
    done = last_screen(h.press(owner, "st:rm:80:0"))
    assert done.text.startswith("✅ @leaving больше не менеджер.") and "@leaving" not in done.text.split("\n\n", 1)[1]
    assert _role(settings, 80) is None

    # old buttons of the removed manager fail authorization
    for data in ("st:pending:0:0", f"st:cp:{order.id}:0", f"st:receipt:{order.id}:0", f"mg:confirm:{order.id}", f"mg:lreject:{order.id}"):
        calls = h.press(manager, data)
        assert _alerts(calls)[0].text == texts.MGR_NO_ACCESS, data
        assert not screens(calls)
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value
    assert _menu(h, manager) == CUSTOMER_MENU


def test_the_owner_can_never_be_removed(h, settings, ids):
    _add_manager(h, 82)
    calls = h.press(make_user(OWNER), f"st:rm:{OWNER}:0")
    assert _alerts(calls)[0].text == texts.STAFF_REMOVE_FAILED
    assert _role(settings, OWNER) == "owner"
    assert "➖ ID 999" not in [t for t, _ in buttons(last_screen(h.press(make_user(OWNER), "st:rmlist:0:0")))]
    conn = _conn(settings)
    try:
        assert staff.deactivate_manager(conn, PROFILE.bot_key, actor_id=OWNER, target_id=OWNER) == "self"
        assert staff.deactivate_manager(conn, PROFILE.bot_key, actor_id=82, target_id=OWNER) == "not_owner"
        assert staff.deactivate_manager(conn, PROFILE.bot_key, actor_id=82, target_id=82) == "not_owner"
    finally:
        conn.close()


def test_one_owner_cannot_remove_another_owner(settings, ids):
    h = BotHarness(settings, profile=PROFILE, manager_ids={OWNER, 998}, owner_id=998)
    try:
        conn = _conn(settings)
        try:
            staff.bootstrap(conn, PROFILE.bot_key, {OWNER}, owner_id=OWNER)  # a second explicit owner
            assert staff.deactivate_manager(conn, PROFILE.bot_key, actor_id=OWNER, target_id=998) == "is_owner"
        finally:
            conn.close()
        assert _role(settings, 998) == "owner"
    finally:
        h.close()


# ------------------------------------------------- BACKWARD COMPATIBILITY


def test_the_sole_configured_manager_becomes_owner_and_still_gets_receipts(h, settings, ids):
    assert _role(settings, OWNER) == "owner"
    order, calls = _to_review(h, make_user(90), ids)
    card = sent_to(calls, OWNER, SendMessage)[0]
    assert card.text.startswith(f"🆕 Новая заявка {order.public_number}")
    assert buttons(card)[:2] == [("✅ Оплата поступила", f"mg:confirm:{order.id}"), ("❌ Оплата не поступила", f"mg:reject:{order.id}")]
    h.press(make_user(OWNER), f"mg:confirm:{order.id}")  # the existing callback, unchanged
    assert _order(settings, order.id).status == OrderStatus.PAID.value


def test_several_configured_managers_and_no_owner_choose_nobody(settings, ids, caplog):
    with caplog.at_level(logging.WARNING):
        h = BotHarness(settings, profile=PROFILE, manager_ids={100, 101})
    try:
        assert (_role(settings, 100), _role(settings, 101)) == ("manager", "manager")
        assert "TELEGRAM_BOT_OWNER_ID" in caplog.text
    finally:
        h.close()
    h = BotHarness(settings, profile=PROFILE, manager_ids={100, 101}, owner_id=101)
    try:
        assert (_role(settings, 100), _role(settings, 101)) == ("manager", "owner")
    finally:
        h.close()


def test_env_list_changes_and_owner_decisions_across_restarts(settings, ids):
    first = BotHarness(settings, profile=PROFILE, manager_ids={OWNER, 102}, owner_id=OWNER)
    first.press(make_user(OWNER), "st:rm:102:0")  # the owner removes a configured manager
    first.close()
    second = BotHarness(settings, profile=PROFILE, manager_ids={OWNER, 102, 103}, owner_id=OWNER)
    second.close()
    assert _role(settings, 102) is None  # the owner's removal survives a restart
    assert _role(settings, 103) == "manager"  # newly configured
    third = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, owner_id=OWNER)
    third.close()
    assert _role(settings, 103) is None  # dropped from the env list -> revoked, as before
    assert _role(settings, OWNER) == "owner"


def test_owner_id_config_validation():
    assert parse_owner_id(None) is None and parse_owner_id(" ") is None and parse_owner_id("5712994689") == 5712994689
    for bad in ("abc", "-1", "0", "1,2"):
        with pytest.raises(BotConfigError):
            parse_owner_id(bad)


# ------------------------------------------------------------------- RESTART


def test_roles_invites_and_pending_orders_survive_a_restart(settings, ids):
    first = BotHarness(settings, profile=PROFILE, manager_ids={OWNER})
    _add_manager(first, 110)
    pending_link = _invite_link(first)
    order, _ = _to_review(first, make_user(111), ids)
    first.close()

    second = BotHarness(settings, profile=PROFILE, manager_ids={OWNER})
    try:
        assert _menu(second, make_user(110)) == MANAGER_MENU
        assert _menu(second, make_user(OWNER)) == OWNER_MENU
        second.send_text(make_user(112), f"/start {_token(pending_link)}")
        assert _role(settings, 112) == "manager"
        pending = last_screen(second.press(make_user(110), "st:pending:0:0"))
        assert f"st:cp:{order.id}:0" in [d for _, d in buttons(pending)]
        second.press(make_user(110), f"mg:lconfirm:{order.id}")
        assert _order(settings, order.id).status == OrderStatus.PAID.value
    finally:
        second.close()


def test_order_files_are_untouched_by_staff_screens(h, settings, ids):
    manager = _add_manager(h, 120)
    order, _ = _to_review(h, make_user(121), ids)
    conn = _conn(settings)
    try:
        before = order_files.list_files(conn, order.id)
    finally:
        conn.close()
    for data in ("st:pending:0:0", f"st:cp:{order.id}:0", f"st:receipt:{order.id}:0", "st:orders:0:0", f"st:co:{order.id}:0"):
        h.press(manager, data)
    conn = _conn(settings)
    try:
        assert order_files.list_files(conn, order.id) == before
    finally:
        conn.close()
