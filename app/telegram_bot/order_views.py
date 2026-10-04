"""Customer-facing order screens (after the order exists): payment details,
"чек получен", "оплата подтверждена", "полис готов".

The amount is ALWAYS the order's own stored price_customer_minor -- never
a freshly calculated price: an admin price change after the order was
created must not change what this customer pays.
"""

import logging
import sqlite3

from aiogram.types import InlineKeyboardMarkup

from app.formatting import format_phone
from app.orders import files as order_files
from app.orders.models import Order
from app.orders.payment import mark_awaiting_payment
from app.orders.repository import get_order_by_id
from app.orders.state_machine import OrderStatus
from app.settings import Settings
from app.telegram_bot import texts
from app.telegram_bot.keyboards import OrderCb, keyboard
from app.telegram_bot.profile import BotProfile

logger = logging.getLogger(__name__)

# While one of these is the customer's current order, a new checkout can't
# be started (it would create a second unpaid order).
UNFINISHED_STATUSES = {OrderStatus.DATA_COMPLETED.value, OrderStatus.AWAITING_PAYMENT.value, OrderStatus.PAYMENT_REVIEW.value}
RECEIPT_STATUSES = {OrderStatus.AWAITING_PAYMENT.value, OrderStatus.PAYMENT_REVIEW.value}
POLICY_DELIVERED_STATUSES = {OrderStatus.POLICY_READY.value, OrderStatus.COMPLETED.value}


def customer_order(conn: sqlite3.Connection, profile: BotProfile, telegram_user_id: int, order_id) -> Order | None:
    """The order, only if it is THIS bot's Telegram order of THIS user --
    an order id from a draft or a button is never trusted on its own."""
    if not order_id:
        return None
    order = get_order_by_id(conn, int(order_id))
    if order is None or order.channel != "telegram" or order.bot_key != profile.bot_key:
        return None
    if order.telegram_user_id != telegram_user_id:
        return None
    if order.is_operator_order:
        # A policy a manager issued for someone else: never a customer order of
        # the manager (no payment screen, no receipts -- app.telegram_bot.operator_issue).
        return None
    if order.status == OrderStatus.DATA_COMPLETED.value:
        # Only possible if the bot stopped between creating the order and
        # moving it on; finish that step now.
        order = mark_awaiting_payment(conn, order.id, note="telegram checkout resumed").order
    return order


def amount_rub(order: Order) -> str:
    return texts.format_rub(order.price_customer_minor // 100)


def payment_details_text(settings: Settings, order: Order) -> tuple[str, bool]:
    """(text, whether real details were shown). Incomplete configuration
    shows NO partial details -- only "временно недоступна" -- and logs just
    which variables are missing (never any value)."""
    payment = settings.telegram_payment
    if not payment.is_complete:
        logger.warning(
            "Telegram payment details not shown for order %s: missing %s",
            order.public_number,
            ", ".join(payment.missing_variables()),
        )
        return texts.PAYMENT_UNAVAILABLE.format(number=order.public_number), False
    lines = [
        texts.PAYMENT_TITLE.format(number=order.public_number),
        "",
        f"Сумма: {amount_rub(order)} ₽",
        "",
        f"Банк: {payment.bank_name}",
        "Перевод по номеру телефона:",
        format_phone(payment.phone_number.get_secret_value()),
        "",
        f"Получатель: {payment.recipient.get_secret_value()}",
    ]
    if payment.instructions:
        lines += ["", payment.instructions]
    lines += ["", texts.PAYMENT_AFTER]
    return "\n".join(lines), True


def order_keyboard(conn: sqlite3.Connection, order: Order) -> InlineKeyboardMarkup:
    status = order.status
    rows = []
    if status == OrderStatus.AWAITING_PAYMENT.value:
        rows.append([(texts.BTN_SEND_RECEIPT, OrderCb(action="receipt", order_id=order.id))])
        rows.append([(texts.BTN_SHOW_DETAILS, OrderCb(action="details", order_id=order.id))])
    elif status == OrderStatus.PAYMENT_REVIEW.value:
        rows.append([(texts.BTN_SEND_ANOTHER_RECEIPT, OrderCb(action="receipt", order_id=order.id))])
        rows.append([(texts.BTN_SHOW_DETAILS, OrderCb(action="details", order_id=order.id))])
    else:
        if status in POLICY_DELIVERED_STATUSES and order_files.latest_file(conn, order.id, order_files.KIND_POLICY):
            rows.append([(texts.BTN_RESEND_POLICY, OrderCb(action="resend_policy", order_id=order.id))])
        rows.append([(texts.BTN_NEW_PURCHASE, OrderCb(action="new", order_id=order.id))])
    return keyboard(rows)


def order_text(settings: Settings, order: Order) -> tuple[str, bool]:
    """(text, whether payment details were part of it)."""
    number = order.public_number
    status = order.status
    if status == OrderStatus.AWAITING_PAYMENT.value:
        return payment_details_text(settings, order)
    if status == OrderStatus.PAYMENT_REVIEW.value:
        return texts.RECEIPT_RECEIVED.format(number=number), False
    if status in (OrderStatus.PAID.value, OrderStatus.PROCESSING.value):
        return texts.STATUS_PAID.format(number=number), False
    if status in POLICY_DELIVERED_STATUSES:
        return texts.STATUS_POLICY_READY.format(number=number), False
    return texts.STATUS_CANCELLED.format(number=number), False


def rejected_text(settings: Settings, order: Order) -> str:
    details, _ = payment_details_text(settings, order)
    return f"{texts.PAYMENT_REJECTED}\n\n{details}"
