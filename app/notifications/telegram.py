"""Operator-facing Telegram notification for a just-paid order.

Sent exactly once, right after an admin confirms payment (PAYMENT_REVIEW ->
PAID) at /admin/orders (see app.web.admin_routes.post_admin_confirm_payment,
the only caller) -- a human operator still has to manually issue the real
policy from this; PayMaster and tpl.ge policy issuance are NOT integrated at
this stage.

Transport is Telethon (an authorized Telegram USER account), not the Bot
API -- no bot is created or needed. The account is the SAME one already
used by the separate ai-lead-radar project (same TELEGRAM_API_ID/
TELEGRAM_API_HASH/TELEGRAM_PHONE -- it's already a member of the target
chat, see TELEGRAM_OPERATOR_CHAT_ID), but this module ALWAYS opens its own
dedicated .session file (TelegramOperatorSettings.session_path, default
data/sessions/auto_insurance_operator) -- it must never open, copy, or
share ai-lead-radar's reader_live/reader_sync/reader_notifier/inviter
sessions. Two independent sessions for the same account is normal,
supported Telegram behaviour (like being logged in on phone + desktop at
once); opening the SAME .session file from two processes is what risks the
SQLite lock/corruption ai-lead-radar has already hit, and this design never
does that. See app.notifications.authorize_telegram_operator for the
one-time interactive login that creates this session.

MVP transport: one connect -> send -> disconnect per notification (no
persistent daemon/background connection).

This module never decides WHEN to notify and never touches order/payment
state itself -- it only formats a message from an already-loaded Order and
sends it. notify_operator_order_paid() is deliberately best-effort: it
NEVER raises, so a Telegram outage can never affect the payment status it's
reporting on. Credentials come from app.settings.TelegramOperatorSettings --
never hardcoded, never logged (the phone number and api_hash are treated as
sensitive and never appear in any log/exception message here).
"""

import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient

from app.orders.models import Order
from app.web.templating import _format_rub

logger = logging.getLogger(__name__)

# Display labels only, for the destination countries this checkout
# currently supports (see app.web.checkout_routes.COUNTRY_CODE) -- not a
# new stored field. Falls back to the raw code for anything unrecognized
# rather than guessing.
_COUNTRY_NAMES = {"GE": "Грузия", "AM": "Армения", "TR": "Турция"}


class TelegramNotifyError(Exception):
    """Raised for any failure sending the Telegram message (connection,
    unauthorized session, send failure). The message never includes the
    phone number or api_hash -- see send_message."""


async def _send_via_telethon(*, api_id: int, api_hash: str, session_path: Path, chat_id: int | str, text: str) -> None:
    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise TelegramNotifyError(
                f"Telegram session at {session_path} is not authorized -- run "
                "`python -m app.notifications.authorize_telegram_operator` once"
            )
        await client.send_message(chat_id, text)
    finally:
        await client.disconnect()


def send_message(*, api_id: int, api_hash: str, session_path: Path, chat_id: int | str, text: str) -> None:
    """Connect using auto-insurance's own dedicated Telethon session, send
    one message, disconnect. Raises TelegramNotifyError on any failure;
    never logs the api_hash or phone number (neither is even accepted by
    this function -- a valid session file needs only api_id/api_hash to
    reopen, no phone re-entry)."""
    try:
        asyncio.run(
            _send_via_telethon(api_id=api_id, api_hash=api_hash, session_path=session_path, chat_id=chat_id, text=text)
        )
    except TelegramNotifyError:
        raise
    except Exception as exc:
        raise TelegramNotifyError(f"Telegram send failed: {type(exc).__name__}") from exc


def _line(label: str, value) -> str | None:
    if value in (None, ""):
        return None
    return f"{label}: {value}"


def _lines(*pairs: tuple[str, object]) -> list[str]:
    return [line for line in (_line(label, value) for label, value in pairs) if line is not None]


def _owner_lines(order: Order) -> list[str]:
    if order.owner_same_as_policyholder:
        return ["Совпадает со страхователем"]
    is_legal = order.owner_entity_type == "legal"
    name_label = "Организация" if is_legal else "ФИО"
    id_label = "Идентификационный код" if is_legal else "Идентификационный номер"
    return _lines(
        (name_label, order.owner_full_name),
        (id_label, order.owner_identifier),
        ("Гражданство", None if is_legal else order.owner_citizenship),
        ("Телефон", order.owner_phone),
        ("Email", order.owner_email),
    )


def _driver_lines(order: Order) -> list[str]:
    if order.driver_same_as_policyholder:
        return ["Совпадает со страхователем"]
    return _lines(
        ("ФИО", order.driver_full_name),
        ("Идентификационный номер", order.driver_identifier),
        ("Гражданство", order.driver_citizenship),
        ("Телефон", order.driver_phone),
        ("Email", order.driver_email),
    )


def format_paid_order_message(order: Order, *, category_name: str | None, period_label: str | None) -> str:
    """Plain-text summary an operator needs to manually issue the policy —
    every field here is read from the existing Order/catalog data, nothing
    invented. Optional fields that are empty are simply omitted (never
    rendered as "Label: None" or "Label: ")."""
    country_name = _COUNTRY_NAMES.get(order.country_code, order.country_code)
    amount = None
    if order.price_customer_minor is not None:
        amount = f"{_format_rub(order.price_customer_minor // 100)} ₽"
    dates = None
    if order.start_date and order.end_date:
        dates = f"{order.start_date.strftime('%d.%m.%Y')} — {order.end_date.strftime('%d.%m.%Y')}"

    sections = [
        "\n".join(["💰 ОПЛАЧЕННЫЙ ЗАКАЗ", order.public_number]),
        "\n".join(
            _lines(
                ("Страна", country_name),
                ("Категория", category_name),
                ("Период", period_label),
                ("Даты", dates),
                ("Сумма", amount),
            )
        ),
        "\n".join(
            ["🚗 АВТОМОБИЛЬ", ""]
            + _lines(
                ("Производитель", order.vehicle_make),
                ("Модель", order.vehicle_model),
                ("Госномер", order.display_registration_number),
                ("VIN / номер шасси", order.display_identifier),
            )
        ),
        "\n".join(
            ["👤 СТРАХОВАТЕЛЬ", ""]
            + _lines(
                ("ФИО", order.full_name),
                ("Идентификационный номер", order.identification_number),
                ("Гражданство", order.citizenship),
            )
        ),
        "\n".join(["📞 КОНТАКТЫ", ""] + [f"{label}: {value}" for label, value in order.contact_rows]),
        "\n".join(["👤 СОБСТВЕННИК", ""] + _owner_lines(order)),
        "\n".join(["👤 ВОДИТЕЛЬ", ""] + _driver_lines(order)),
        "Статус: ОПЛАЧЕНО",
    ]
    return "\n\n".join(sections)


def notify_operator_order_paid(
    *,
    api_id: int | None,
    api_hash: str | None,
    phone: str | None,
    session_path: Path,
    chat_id: int | str | None,
    order: Order,
    category_name: str | None,
    period_label: str | None,
) -> bool:
    """Best-effort operator notification for a just-confirmed PAID order.
    Returns True on success, False on any failure (including "not
    configured") -- NEVER raises. See app.web.admin_routes.
    post_admin_confirm_payment, the only caller: payment status must
    already be committed before this runs, and this function's outcome
    must never change it. Failures are logged (safe classification only,
    never the phone/api_hash) so they're visible in server logs even
    though the payment confirmation itself proceeds either way.

    phone is only checked here as a "is this fully configured" signal
    (mirrors the required TELEGRAM_PHONE env var used for the one-time
    authorization) -- the actual send below needs only api_id/api_hash/
    session_path/chat_id; a valid, already-authorized session never needs
    the phone number again."""
    if not api_id or not api_hash or not phone or not chat_id:
        logger.warning("Telegram operator notification skipped: not configured (order %s)", order.public_number)
        return False

    text = format_paid_order_message(order, category_name=category_name, period_label=period_label)
    try:
        send_message(api_id=api_id, api_hash=api_hash, session_path=session_path, chat_id=chat_id, text=text)
    except TelegramNotifyError as exc:
        logger.warning("Telegram operator notification failed for order %s: %s", order.public_number, exc)
        return False
    return True
