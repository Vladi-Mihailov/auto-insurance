"""Manager side: the order card, payment confirm / "not found", and policy
upload + delivery.

Authorization, on EVERY manager action (callback_data is never trusted):
1. the sender's Telegram user id must be ACTIVE staff (manager or owner)
   of this bot in telegram_bot_staff (app.telegram_bot.staff) -- read at
   execution time, so a removed manager's old buttons stop working at once;
2. the order is loaded from the DB by id;
3. it must be a Telegram-channel order ...
4. ... taken by THIS bot (bot_key);
5. it must be in the status the action expects -- enforced atomically by
   the shared payment service / transition_if (compare-and-set), so a
   repeated tap or another manager's earlier tap is a harmless no-op
   ("Уже обработано").

Policy upload: "📄 Отправить полис" stores an upload context (manager ->
order, expiring) in the DB. The manager's NEXT PDF is re-validated against
that order (still this bot's Telegram order, still PAID/PROCESSING) and the
PDF goes ONLY to the telegram_chat_id stored on that order -- never to a
chat derived from the manager's own chat, a username, or anything typed.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.analytics.repository import log_event
from app.catalog import repository as catalog_repo
from app.formatting import format_phone
from app.notifications import bot_outbox
from app.orders import files as order_files
from app.orders.models import Order
from app.orders.payment import confirm_payment, order_event_properties, reject_payment
from app.orders.repository import get_order_by_id, transition_if
from app.orders.state_machine import OrderStatus
from app.pricing.provider import get_period
from app.settings import Settings
from app.telegram_bot import staff, texts
from app.telegram_bot.keyboards import MgrCb, OpCb, StaffCb
from app.telegram_bot.profile import BotProfile

logger = logging.getLogger(__name__)

UPLOAD_CONTEXT_MINUTES = 30
MAX_POLICY_BYTES = 20 * 1024 * 1024
POLICY_UPLOAD_STATUSES = {OrderStatus.PAID.value, OrderStatus.PROCESSING.value}


# ---------------------------------------------------------------- card


def _vehicle_name_line(label: str, document_name: str | None, catalog_name: str | None) -> str:
    """The document's own brand/model, plus the catalog entry the order
    carries when that differs (the "Other" fallback)."""
    if document_name and document_name != catalog_name:
        return f"{label} {document_name} (в каталоге: {catalog_name or '—'})"
    return f"{label} {catalog_name or '—'}"


def _payment_lines(settings: Settings, order: Order) -> list[str]:
    """Everything the manager compares against the bank before confirming:
    the order's own stored amount, and the requisites the customer was shown."""
    payment = settings.telegram_payment
    lines = [texts.MGR_PAYMENT_HEADER, f"Сумма к получению: {texts.format_rub(order.price_customer_minor // 100)} ₽"]
    if payment.recipient is not None:
        lines.append(f"Получатель: {payment.recipient.get_secret_value()}")
    if payment.bank_name or payment.phone_number is not None:
        phone = format_phone(payment.phone_number.get_secret_value()) if payment.phone_number is not None else ""
        lines.append(" · ".join(part for part in (payment.bank_name, phone) if part))
    return lines


def _origin_lines(conn: sqlite3.Connection, order: Order, client: str) -> list[str]:
    if not order.is_operator_order:
        return ["Клиент:", f"{client}Telegram ID {order.telegram_user_id}", ""]
    member = staff.get_member(conn, order.bot_key, order.created_by_telegram_user_id) if order.created_by_telegram_user_id else None
    label = member.label if member else f"ID {order.created_by_telegram_user_id}"
    return [texts.OP_CARD_ORIGIN.format(label=label), ""]


def _receipt_line(conn: sqlite3.Connection, order: Order) -> str:
    count = len(order_files.list_files(conn, order.id, order_files.KIND_PAYMENT_RECEIPT))
    return texts.MGR_RECEIPTS_SOME.format(count=count) if count else texts.MGR_RECEIPTS_NONE


def card_text(conn: sqlite3.Connection, settings: Settings, order: Order) -> str:
    category = texts.CATEGORY_LABELS.get(order.vehicle_category_code)
    if category is None:
        row = catalog_repo.get_category_by_code(conn, order.vehicle_category_code) if order.vehicle_category_code else None
        category = row.name if row else order.vehicle_category_code
    period = get_period(settings, order.country_code, order.vehicle_category_code, order.period_code) if order.period_code else None
    title = texts.MGR_TITLE_REVIEW if order.status == OrderStatus.PAYMENT_REVIEW.value else texts.MGR_TITLE
    client = f"@{order.telegram_username} · " if order.telegram_username else ""
    vin = order.identifier if order.identifier_type == "vin" else "—"
    chassis = order.identifier if order.identifier_type == "chassis" else "—"
    lines = [
        title.format(number=order.public_number),
        "",
        *_origin_lines(conn, order, client),
        *([] if order.is_operator_order else [*_payment_lines(settings, order), _receipt_line(conn, order), ""]),
        "Страхование:",
        category or "—",
        f"📅 начало {order.start_date:%d.%m.%Y}" if order.start_date else "📅 начало —",
        f"⏱ {period.label if period else order.period_code}",
        f"💰 {texts.format_rub(order.price_customer_minor // 100)} ₽",
        "",
        "Автомобиль:",
        f"Госномер {order.display_registration_number or '—'}",
        f"VIN {vin}",
        f"Шасси {chassis}",
        _vehicle_name_line("Марка", order.vehicle_make_document, order.vehicle_make),
        _vehicle_name_line("Модель", order.vehicle_model_document, order.vehicle_model),
        "",
        "Страхователь:",
        f"ФИО {order.full_name or '—'}",
        f"Гражданство {order.citizenship or '—'}",
        f"Паспорт {order.identification_number or '—'}",
        f"Email {order.contact_email or '—'}",
        f"Телефон {order.contact_phone or '—'}",
        "",
        "Источник:",
        order.acquisition_source or "—",
        "",
        "Статус:",
        texts.MGR_STATUS.get(order.status, order.status),
    ]
    return "\n".join(lines)


def card_keyboard(conn: sqlite3.Connection, order: Order, *, auto_issuance: bool = False) -> InlineKeyboardMarkup | None:
    """auto_issuance -- settings.telegram_bot.tpl_auto_issuance. Off: the
    manual policy upload is the only (primary) action on a paid order."""
    rows: list[list[InlineKeyboardButton]] = []

    def button(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=MgrCb(action=action, order_id=order.id).pack())

    if order.status == OrderStatus.PAYMENT_REVIEW.value:
        rows.append([button(texts.BTN_MGR_CONFIRM, "confirm"), button(texts.BTN_MGR_REJECT, "reject")])
    elif order.status in POLICY_UPLOAD_STATUSES and not auto_issuance:
        rows.append([button(texts.BTN_MGR_UPLOAD_POLICY, "policy")])
        if order.status == OrderStatus.PROCESSING.value and order_files.latest_file(conn, order.id, order_files.KIND_POLICY):
            rows.append([button(texts.BTN_MGR_RESEND_POLICY, "resend_policy")])
    elif order.status in POLICY_UPLOAD_STATUSES:
        # Primary: the tpl.ge issuance (app.telegram_bot.operator_issue);
        # the manual PDF upload stays as an explicit fallback.
        rows.append([InlineKeyboardButton(text=texts.BTN_TPL_START, callback_data=OpCb(a="start", order_id=order.id).pack())])
        rows.append([button(texts.BTN_MGR_SEND_POLICY, "policy")])
        if order.status == OrderStatus.PROCESSING.value and order_files.latest_file(conn, order.id, order_files.KIND_POLICY):
            rows.append([button(texts.BTN_MGR_RESEND_POLICY, "resend_policy")])
    if order.telegram_username:
        rows.append([InlineKeyboardButton(text=texts.BTN_MGR_CLIENT, url=f"https://t.me/{order.telegram_username}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def list_card_keyboard(
    conn: sqlite3.Connection, order: Order, *, back: StaffCb | None = None, auto_issuance: bool = False,
) -> InlineKeyboardMarkup:
    """A card opened from "📋 Заказы" / "⏳ Ожидают оплаты": the same actions
    as the notification card (confirm/reject go through the SAME handler and
    payment service, as "lconfirm"/"lreject" so the card refreshes in
    place), plus "🧾 Показать чек" and the way back."""
    rows: list[list[InlineKeyboardButton]] = []
    if order.is_operator_order:
        # its own issuance screen (TPL link / policy) -- never the customer payment actions
        rows.append([InlineKeyboardButton(text=texts.BTN_OP_STATUS, callback_data=OpCb(a="status", order_id=order.id).pack())])
        back = back or StaffCb(a="orders")
        rows.append([InlineKeyboardButton(text=texts.BTN_STAFF_TO_ORDERS if back.a == "orders" else texts.BTN_STAFF_TO_PENDING, callback_data=back.pack())])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    for row in (card_keyboard(conn, order, auto_issuance=auto_issuance) or InlineKeyboardMarkup(inline_keyboard=[])).inline_keyboard:
        rebuilt = []
        for button in row:
            data = button.callback_data
            if data and data.startswith("mg:confirm:"):
                button = InlineKeyboardButton(text=button.text, callback_data=MgrCb(action="lconfirm", order_id=order.id).pack())
            elif data and data.startswith("mg:reject:"):
                button = InlineKeyboardButton(text=button.text, callback_data=MgrCb(action="lreject", order_id=order.id).pack())
            rebuilt.append(button)
        rows.append(rebuilt)
    if order_files.list_files(conn, order.id, order_files.KIND_PAYMENT_RECEIPT):
        rows.append([InlineKeyboardButton(text=texts.BTN_STAFF_RECEIPT, callback_data=StaffCb(a="receipt", i=order.id).pack())])
    back = back or StaffCb(a="pending")
    label = texts.BTN_STAFF_TO_ORDERS if back.a == "orders" else texts.BTN_STAFF_TO_PENDING
    rows.append([InlineKeyboardButton(text=label, callback_data=back.pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------ upload context


def _now() -> datetime:
    return datetime.now(timezone.utc)


def set_upload_context(conn: sqlite3.Connection, *, bot_key: str, manager_user_id: int, order_id: int) -> None:
    now = _now()
    conn.execute(
        """INSERT INTO telegram_manager_upload_contexts (bot_key, manager_user_id, order_id, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (bot_key, manager_user_id) DO UPDATE SET
               order_id = excluded.order_id, created_at = excluded.created_at, expires_at = excluded.expires_at""",
        (bot_key, manager_user_id, order_id, now.isoformat(), (now + timedelta(minutes=UPLOAD_CONTEXT_MINUTES)).isoformat()),
    )
    conn.commit()


def get_upload_context(conn: sqlite3.Connection, *, bot_key: str, manager_user_id: int) -> tuple[int, bool] | None:
    """(order_id, expired) or None."""
    row = conn.execute(
        "SELECT order_id, expires_at FROM telegram_manager_upload_contexts WHERE bot_key = ? AND manager_user_id = ?",
        (bot_key, manager_user_id),
    ).fetchone()
    if row is None:
        return None
    return row["order_id"], datetime.fromisoformat(row["expires_at"]) < _now()


def clear_upload_context(conn: sqlite3.Connection, *, bot_key: str, manager_user_id: int) -> None:
    conn.execute(
        "DELETE FROM telegram_manager_upload_contexts WHERE bot_key = ? AND manager_user_id = ?", (bot_key, manager_user_id)
    )
    conn.commit()


# ------------------------------------------------------------ helpers


def _this_bots_order(conn: sqlite3.Connection, profile: BotProfile, order_id: int) -> Order | None:
    order = get_order_by_id(conn, order_id)
    if order is None or order.channel != "telegram" or order.bot_key != profile.bot_key or order.telegram_chat_id is None:
        return None
    return order


async def _refresh_card(
    callback: CallbackQuery, conn: sqlite3.Connection, settings: Settings, order: Order, *, from_list: bool = False
) -> None:
    """Re-render the card the manager pressed (e.g. after a stale tap)."""
    if isinstance(callback.message, Message):
        auto = settings.telegram_bot.tpl_auto_issuance
        markup = (
            list_card_keyboard(conn, order, auto_issuance=auto) if from_list else card_keyboard(conn, order, auto_issuance=auto)
        )
        try:
            await callback.message.edit_text(card_text(conn, settings, order), reply_markup=markup)
        except TelegramAPIError:
            pass  # "message is not modified" etc. -- the card is already current


def _log_order_event(conn: sqlite3.Connection, name: str, order: Order, **extra) -> None:
    log_event(conn, session_id=order.session_id, order_id=order.id, event_name=name, properties={**order_event_properties(order), **extra})


async def deliver_policy(bot: Bot, conn: sqlite3.Connection, order: Order, *, manager_chat_id: int, outbox) -> bool:
    """Sends the order's latest policy PDF to the order's OWN customer chat.
    Only after Telegram accepts it does the order become POLICY_READY; on
    failure the policy stays attached (status PROCESSING) and the manager
    gets a retry button."""
    policy = order_files.latest_file(conn, order.id, order_files.KIND_POLICY)
    if policy is None:
        return False
    try:
        await bot.send_document(
            order.telegram_chat_id, policy.telegram_file_id, caption=texts.POLICY_CAPTION.format(number=order.public_number)
        )
    except TelegramAPIError as exc:
        logger.warning("Policy delivery failed for order %s (%s)", order.public_number, type(exc).__name__)
        retry = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.BTN_MGR_RESEND_POLICY, callback_data=MgrCb(action="resend_policy", order_id=order.id).pack())]]
        )
        await bot.send_message(manager_chat_id, texts.MGR_POLICY_SEND_FAILED.format(number=order.public_number), reply_markup=retry)
        return False

    history_id = transition_if(
        conn, order.id, OrderStatus.PROCESSING, OrderStatus.POLICY_READY, note="policy delivered to customer", commit=False
    )
    jobs = []
    if history_id is not None:
        order = get_order_by_id(conn, order.id)
        jobs = bot_outbox.enqueue_card_updates(conn, order, history_id=history_id)
        _log_order_event(conn, "bot_policy_delivered", order)  # commits the transition + jobs together
    conn.commit()
    await bot.send_message(manager_chat_id, texts.MGR_POLICY_SENT.format(number=order.public_number))
    if outbox is not None and jobs:
        await outbox.process(bot, conn, job_ids=jobs)
    return True


# ----------------------------------------------------------- handlers


async def on_manager_action(
    callback: CallbackQuery, callback_data: MgrCb, conn: sqlite3.Connection, settings: Settings,
    profile: BotProfile, outbox=None,
):
    manager_id = callback.from_user.id
    if not staff.is_staff(conn, profile.bot_key, manager_id):
        logger.warning("Rejected manager action %r on order id %s from a non-manager user", callback_data.action, callback_data.order_id)
        await callback.answer(texts.MGR_NO_ACCESS, show_alert=True)
        return
    order = _this_bots_order(conn, profile, callback_data.order_id)
    if order is not None and order.is_operator_order:
        order = None  # operator orders have their own screen (app.telegram_bot.operator_issue)
    if order is None:
        logger.warning("Manager action %r on unknown/foreign order id %s", callback_data.action, callback_data.order_id)
        await callback.answer(texts.MGR_ORDER_NOT_FOUND, show_alert=True)
        return

    action = callback_data.action
    from_list = action in ("lconfirm", "lreject")  # the same action, pressed on a list-opened card
    if from_list:
        action = action[1:]
    actor = f"telegram manager {manager_id}"
    if action in ("confirm", "reject"):
        # ONE implementation for every route (notification card, list card,
        # web admin): the payment service's compare-and-set transition.
        result = (confirm_payment if action == "confirm" else reject_payment)(conn, order.id, actor=actor)
        if not result.changed:
            await callback.answer(texts.MGR_ALREADY_DONE, show_alert=True)
            await _refresh_card(callback, conn, settings, result.order, from_list=from_list)
            return
        await callback.answer(texts.MGR_CONFIRMED if action == "confirm" else texts.MGR_REJECTED)
        if outbox is not None:
            await outbox.process(callback.bot, conn, job_ids=result.outbox_job_ids)
        if from_list:
            # Tracked notification cards are updated by the outbox; this one isn't tracked.
            await _refresh_card(callback, conn, settings, result.order, from_list=True)
        if action == "confirm" and settings.telegram_bot.tpl_auto_issuance:
            # Our payment confirmation is committed and acknowledged -- only now
            # the tpl.ge issuance starts (the same one as for operator orders).
            # Off (the default): nothing is sent to tpl.ge; the order stays
            # PAID and the card (updated by the outbox) offers the manual upload.
            from app.telegram_bot.operator_issue import after_payment_confirmed

            await after_payment_confirmed(callback.bot, conn, settings, result.order, manager_chat_id=manager_id, actor=actor)
        return

    if action == "policy":
        if order.status not in POLICY_UPLOAD_STATUSES:
            await callback.answer(texts.MGR_ALREADY_DONE, show_alert=True)
            await _refresh_card(callback, conn, settings, order)
            return
        set_upload_context(conn, bot_key=profile.bot_key, manager_user_id=manager_id, order_id=order.id)
        _log_order_event(conn, "bot_policy_upload_started", order)
        cancel = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.BTN_MGR_CANCEL_UPLOAD, callback_data=MgrCb(action="cancel_upload", order_id=order.id).pack())]]
        )
        await callback.bot.send_message(
            manager_id, texts.MGR_UPLOAD_PROMPT.format(number=order.public_number, minutes=UPLOAD_CONTEXT_MINUTES), reply_markup=cancel
        )
        await callback.answer()
        return

    if action == "resend_policy":
        if order.status != OrderStatus.PROCESSING.value or not order_files.latest_file(conn, order.id, order_files.KIND_POLICY):
            await callback.answer(texts.MGR_ALREADY_DONE, show_alert=True)
            await _refresh_card(callback, conn, settings, order)
            return
        await callback.answer()
        await deliver_policy(callback.bot, conn, order, manager_chat_id=manager_id, outbox=outbox)
        return

    if action == "cancel_upload":
        clear_upload_context(conn, bot_key=profile.bot_key, manager_user_id=manager_id)
        await callback.answer(texts.MGR_UPLOAD_CANCELLED)
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(texts.MGR_UPLOAD_CANCELLED)
            except TelegramAPIError:
                pass
        return

    await callback.answer()


async def on_manager_document(
    message: Message, conn: sqlite3.Connection, settings: Settings, profile: BotProfile, outbox=None
):
    """A document from a manager WITH an active upload context is the policy
    for that context's order; anything else falls through to the normal
    (customer) handlers -- a manager can be a customer too."""
    manager_id = message.from_user.id
    if not staff.is_staff(conn, profile.bot_key, manager_id):
        raise SkipHandler()
    context = get_upload_context(conn, bot_key=profile.bot_key, manager_user_id=manager_id)
    if context is None:
        raise SkipHandler()
    order_id, expired = context
    if expired:
        clear_upload_context(conn, bot_key=profile.bot_key, manager_user_id=manager_id)
        await message.answer(texts.MGR_UPLOAD_EXPIRED)
        return

    order = _this_bots_order(conn, profile, order_id)
    if order is None or order.status not in POLICY_UPLOAD_STATUSES:
        clear_upload_context(conn, bot_key=profile.bot_key, manager_user_id=manager_id)
        status = texts.MGR_STATUS.get(order.status, order.status) if order else "—"
        await message.answer(texts.MGR_UPLOAD_NOT_ALLOWED.format(number=order.public_number if order else "—", status=status))
        return

    document = message.document
    name = (document.file_name or "").lower()
    if document.mime_type != "application/pdf" or not name.endswith(".pdf"):
        await message.answer(texts.MGR_UPLOAD_NEED_PDF)  # context kept: send the right file
        return
    if document.file_size is not None and document.file_size > MAX_POLICY_BYTES:
        await message.answer(texts.MGR_UPLOAD_TOO_BIG)
        return

    # Persist first: the policy is attached to the order before any send.
    order_files.add_file(
        conn,
        order_id=order.id,
        kind=order_files.KIND_POLICY,
        bot_key=profile.bot_key,
        telegram_file_id=document.file_id,
        telegram_file_unique_id=document.file_unique_id,
        mime_type=document.mime_type,
        file_size=document.file_size,
        telegram_media_type="document",
    )
    if order.status == OrderStatus.PAID.value:
        transition_if(conn, order.id, OrderStatus.PAID, OrderStatus.PROCESSING, note=f"policy uploaded by telegram manager {manager_id}")
    clear_upload_context(conn, bot_key=profile.bot_key, manager_user_id=manager_id)
    await deliver_policy(message.bot, conn, get_order_by_id(conn, order.id), manager_chat_id=manager_id, outbox=outbox)


def build_manager_router() -> Router:
    """Registered BEFORE the customer router: manager callbacks are answered
    for anyone (with "Нет доступа" when not a manager), and a manager's
    policy PDF is consumed before customer receipt handling sees it."""
    router = Router(name="manager")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_manager_action, MgrCb.filter())
    router.message.register(on_manager_document, F.document)
    from app.telegram_bot import operator_issue, staff_panel, staff_prices  # they import this module's card helpers

    staff_panel.register(router)
    operator_issue.register_staff_side(router)
    staff_prices.register(router)
    return router
