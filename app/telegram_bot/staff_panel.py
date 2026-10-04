"""Manager / owner screens inside the SAME bot customers use.

- "📋 Заказы": this bot's recent orders, paged;
- "⏳ Ожидают оплаты": orders whose receipt waits for a manager's check
  (PAYMENT_REVIEW), oldest first, paged;
- an order card (the same card text managers get with a receipt), with
  "🧾 Показать чек" and the SAME "✅ Оплата поступила" / "❌ Оплата не
  поступила" buttons -- handled by app.telegram_bot.managers.on_manager_action
  and the shared payment service, never a second implementation;
- "👥 Менеджеры" (owner only): the staff list, a one-time invite link, and
  removing a manager.

Authorization is checked on EVERY press against telegram_bot_staff (the
keyboard only decides what is shown): no role -> "Нет доступа" and nothing
about any order is revealed; owner screens additionally require the owner
role.
"""

import logging
import sqlite3

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.orders import files as order_files
from app.orders.repository import count_bot_orders, list_bot_orders, was_payment_rejected
from app.orders.state_machine import OrderStatus
from app.settings import Settings
from app.telegram_bot import staff, texts
from app.telegram_bot.keyboards import MenuCb, PricesCb, StaffCb
from app.telegram_bot.managers import _this_bots_order, card_text, list_card_keyboard
from app.telegram_bot.profile import BotProfile

logger = logging.getLogger(__name__)

PAGE_SIZE = 8
PENDING_STATUSES = [OrderStatus.PAYMENT_REVIEW.value]
OWNER_ACTIONS = {"managers", "invite", "rmlist", "rmask", "rm"}


def staff_menu_rows(role: str | None) -> list[list[tuple[str, StaffCb | PricesCb]]]:
    """The extra main-menu rows for staff (nothing for a customer). Both
    owner and manager see "💰 Цены" -- price editing is not owner-only (see
    app.telegram_bot.staff_prices, re-checked server-side regardless of what
    this keyboard shows)."""
    if role is None:
        return []
    rows = [
        [(texts.BTN_STAFF_ORDERS, StaffCb(a="orders"))],
        [(texts.BTN_STAFF_PENDING, StaffCb(a="pending"))],
        [(texts.BTN_STAFF_PRICES, PricesCb(a="menu"))],
    ]
    if role == staff.ROLE_OWNER:
        rows.append([(texts.BTN_STAFF_MANAGERS, StaffCb(a="managers"))])
    return rows


def _markup(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button if isinstance(button, InlineKeyboardButton) else InlineKeyboardButton(text=button[0], callback_data=button[1].pack())
                for button in row
            ]
            for row in rows
            if row
        ]
    )


async def _show(callback: CallbackQuery, text: str, rows) -> None:
    markup = _markup(rows)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)


def _amount(order) -> str:
    return texts.format_rub(order.price_customer_minor // 100) if order.price_customer_minor is not None else "—"


def _short_status(conn: sqlite3.Connection, order) -> str:
    if order.is_operator_order:
        return texts.OP_SHORT_STATUS.get(order.status, order.status)
    if order.status == OrderStatus.AWAITING_PAYMENT.value and was_payment_rejected(conn, order.id):
        return texts.STAFF_REJECTED_SHORT
    return texts.STAFF_STATUS_SHORT.get(order.status, order.status)


def _pager(action: str, page: int, total: int) -> list[tuple[str, StaffCb]]:
    row = []
    if page > 0:
        row.append((texts.BTN_PAGE_PREV, StaffCb(a=action, p=page - 1)))
    if (page + 1) * PAGE_SIZE < total:
        row.append((texts.BTN_PAGE_NEXT, StaffCb(a=action, p=page + 1)))
    return row


# ------------------------------------------------------------- screens


def menu_screen(role: str) -> tuple[str, list]:
    return texts.STAFF_MENU, [*staff_menu_rows(role), [(texts.BTN_APPLY, MenuCb(action="apply"))]]


def orders_screen(conn: sqlite3.Connection, profile: BotProfile, page: int) -> tuple[str, list]:
    total = count_bot_orders(conn, bot_key=profile.bot_key)
    page = max(0, min(page, max(0, (total - 1) // PAGE_SIZE)))
    orders = list_bot_orders(conn, bot_key=profile.bot_key, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    if not orders:
        return texts.STAFF_ORDERS_EMPTY, [[(texts.BTN_STAFF_BACK, StaffCb(a="menu"))]]
    rows = [[(f"{o.public_number} · {_short_status(conn, o)}", StaffCb(a="co", i=o.id, p=page))] for o in orders]
    rows += [_pager("orders", page, total), [(texts.BTN_STAFF_BACK, StaffCb(a="menu"))]]
    return f"{texts.STAFF_ORDERS_TITLE.format(page=page + 1)}\n\n{texts.STAFF_PAGE_HINT}", rows


def pending_screen(conn: sqlite3.Connection, profile: BotProfile, page: int) -> tuple[str, list]:
    total = count_bot_orders(conn, bot_key=profile.bot_key, statuses=PENDING_STATUSES)
    page = max(0, min(page, max(0, (total - 1) // PAGE_SIZE)))
    orders = list_bot_orders(
        conn, bot_key=profile.bot_key, statuses=PENDING_STATUSES, limit=PAGE_SIZE, offset=page * PAGE_SIZE, oldest_first=True
    )
    if not orders:
        return texts.STAFF_PENDING_EMPTY, [[(texts.BTN_STAFF_BACK, StaffCb(a="menu"))]]
    rows = [
        [(f"{o.public_number} · {o.full_name or '—'} · {_amount(o)} ₽", StaffCb(a="cp", i=o.id, p=page))] for o in orders
    ]
    rows += [_pager("pending", page, total), [(texts.BTN_STAFF_BACK, StaffCb(a="menu"))]]
    return f"{texts.STAFF_PENDING_TITLE.format(count=total)}\n\n{texts.STAFF_PAGE_HINT}", rows


def managers_screen(conn: sqlite3.Connection, profile: BotProfile, actor_id: int, notice: str | None = None) -> tuple[str, list]:
    members = staff.active_staff(conn, profile.bot_key)
    lines = [texts.STAFF_MANAGERS_TITLE, ""]
    for member in members:
        mark = texts.STAFF_OWNER_MARK if member.role == staff.ROLE_OWNER else texts.STAFF_MANAGER_MARK
        lines.append(f"{mark} {member.label}")
    rows = [[(texts.BTN_STAFF_ADD, StaffCb(a="invite"))]]
    if _removable(members, actor_id):
        rows.append([(texts.BTN_STAFF_REMOVE, StaffCb(a="rmlist"))])
    rows.append([(texts.BTN_STAFF_BACK, StaffCb(a="menu"))])
    text = "\n".join(lines)
    return (f"{notice}\n\n{text}" if notice else text), rows


def _removable(members, actor_id: int):
    return [m for m in members if m.role == staff.ROLE_MANAGER and m.telegram_user_id != actor_id]


# ------------------------------------------------------------- handler


async def on_staff(
    callback: CallbackQuery, callback_data: StaffCb, conn: sqlite3.Connection, settings: Settings, profile: BotProfile, bot: Bot
):
    user_id = callback.from_user.id
    private = callback.message is not None and callback.message.chat.type == ChatType.PRIVATE
    role = staff.role_of(conn, profile.bot_key, user_id) if private else None
    action = callback_data.a
    if role is None:
        logger.warning("Rejected staff screen %r from a non-staff user", action)
        await callback.answer(texts.MGR_NO_ACCESS, show_alert=True)
        return
    if action in OWNER_ACTIONS and role != staff.ROLE_OWNER:
        logger.warning("Rejected owner-only staff action %r from a manager", action)
        await callback.answer(texts.STAFF_OWNER_ONLY, show_alert=True)
        return
    staff.touch_username(conn, profile.bot_key, user_id, callback.from_user.username)

    if action == "menu":
        await _show(callback, *menu_screen(role))
    elif action == "orders":
        await _show(callback, *orders_screen(conn, profile, callback_data.p))
    elif action == "pending":
        await _show(callback, *pending_screen(conn, profile, callback_data.p))
    elif action in ("co", "cp"):
        order = _this_bots_order(conn, profile, callback_data.i)
        if order is None:
            await callback.answer(texts.MGR_ORDER_NOT_FOUND, show_alert=True)
            return
        back = StaffCb(a="orders" if action == "co" else "pending", p=callback_data.p)
        markup = list_card_keyboard(conn, order, back=back, auto_issuance=settings.telegram_bot.tpl_auto_issuance)
        try:
            await callback.message.edit_text(card_text(conn, settings, order), reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                await bot.send_message(user_id, card_text(conn, settings, order), reply_markup=markup)
    elif action == "receipt":
        await _send_receipts(callback, conn, profile, bot, callback_data.i)
        return
    elif action == "managers":
        await _show(callback, *managers_screen(conn, profile, user_id))
    elif action == "invite":
        await _invite(callback, conn, profile, bot)
    elif action == "rmlist":
        removable = _removable(staff.active_staff(conn, profile.bot_key), user_id)
        if not removable:
            await _show(callback, texts.STAFF_REMOVE_NONE, [[(texts.BTN_STAFF_TO_MANAGERS, StaffCb(a="managers"))]])
        else:
            rows = [[(f"➖ {m.label}", StaffCb(a="rmask", i=m.telegram_user_id))] for m in removable]
            rows.append([(texts.BTN_STAFF_TO_MANAGERS, StaffCb(a="managers"))])
            await _show(callback, texts.STAFF_REMOVE_PICK, rows)
    elif action == "rmask":
        member = staff.get_member(conn, profile.bot_key, callback_data.i)
        if member is None or not member.active or member.role != staff.ROLE_MANAGER or member.telegram_user_id == user_id:
            await callback.answer(texts.STAFF_REMOVE_FAILED, show_alert=True)
            await _show(callback, *managers_screen(conn, profile, user_id))
            return
        await _show(
            callback,
            texts.STAFF_REMOVE_ASK.format(label=member.label),
            [[(texts.BTN_STAFF_REMOVE_CONFIRM, StaffCb(a="rm", i=member.telegram_user_id))],
             [(texts.BTN_STAFF_TO_MANAGERS, StaffCb(a="managers"))]],
        )
    elif action == "rm":
        member = staff.get_member(conn, profile.bot_key, callback_data.i)
        result = staff.deactivate_manager(conn, profile.bot_key, actor_id=user_id, target_id=callback_data.i)
        if result != "removed":
            await callback.answer(texts.STAFF_REMOVE_FAILED, show_alert=True)
            await _show(callback, *managers_screen(conn, profile, user_id))
            return
        logger.info("Manager removed by the owner (bot_key=%s)", profile.bot_key)
        await _show(callback, *managers_screen(conn, profile, user_id, notice=texts.STAFF_REMOVED.format(label=member.label)))
    await callback.answer()


async def _send_receipts(callback: CallbackQuery, conn: sqlite3.Connection, profile: BotProfile, bot: Bot, order_id: int) -> None:
    order = _this_bots_order(conn, profile, order_id)
    if order is None:
        await callback.answer(texts.MGR_ORDER_NOT_FOUND, show_alert=True)
        return
    receipts = order_files.list_files(conn, order.id, order_files.KIND_PAYMENT_RECEIPT)
    if not receipts:
        await callback.answer(texts.STAFF_NO_RECEIPT, show_alert=True)
        return
    await callback.answer()
    caption = texts.MGR_FILE_CAPTIONS["payment_receipt"].format(
        number=order.public_number, amount=_amount(order), name=order.full_name or "—"
    )
    for receipt in receipts:
        # Only to the pressing staff member's own private chat.
        if receipt.telegram_media_type == "photo":
            await bot.send_photo(callback.from_user.id, receipt.telegram_file_id, caption=caption)
        else:
            await bot.send_document(callback.from_user.id, receipt.telegram_file_id, caption=caption)


async def _invite(callback: CallbackQuery, conn: sqlite3.Connection, profile: BotProfile, bot: Bot) -> None:
    username = profile.username
    if not username:
        try:
            username = (await bot.me()).username
        except TelegramAPIError:
            username = None
    back = [[(texts.BTN_STAFF_TO_MANAGERS, StaffCb(a="managers"))]]
    if not username:
        await _show(callback, texts.STAFF_INVITE_NO_USERNAME, back)
        return
    token, expires = staff.create_invite(conn, profile.bot_key, created_by=callback.from_user.id)
    link = f"https://t.me/{username}?start={staff.INVITE_PREFIX}{token}"
    logger.info("Manager invite created (bot_key=%s)", profile.bot_key)  # never the token
    await _show(callback, texts.STAFF_INVITE.format(link=link, expires=f"{expires:%d.%m.%Y %H:%M}"), back)


def register(router: Router) -> None:
    router.callback_query.register(on_staff, StaffCb.filter())
