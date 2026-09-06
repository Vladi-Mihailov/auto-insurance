"""Operator-facing Telegram notifications for the checkout/payment funnel.

Three distinct notifications, three distinct moments, all funneled through
the SAME Telethon transport (_notify_operator/send_message below) -- never a
second Telegram client/service:

1. notify_operator_new_order -- sent once, right after post_policyholder
   creates the Order (DATA_COMPLETED) -- see app.web.checkout_routes.
   post_policyholder, the only caller. Lets the operator see a submission
   immediately, before the customer has even reached the payment screen.
2. notify_operator_payment_claimed -- sent once, right after the customer
   clicks "Я оплатил" (AWAITING_PAYMENT -> PAYMENT_REVIEW) -- see
   app.web.routes.post_confirm_payment, the only caller. The most important
   one operationally: this is what tells a human to go check their bank
   statement.
3. notify_operator_order_paid -- sent once, right after an admin confirms
   payment (PAYMENT_REVIEW -> PAID) at /admin/orders (see
   app.web.admin_routes.post_admin_confirm_payment, the only caller) -- a
   human operator still has to manually issue the real policy from this;
   PayMaster and tpl.ge policy issuance are NOT integrated at this stage.

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
sends it. All three notify_operator_* functions are deliberately
best-effort: none of them ever raise, so a Telegram outage can never affect
the order/payment state each is reporting on (see _notify_operator, the
shared send-and-log path all three go through). Credentials come from
app.settings.TelegramOperatorSettings --
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
# currently supports (see app.web.checkout_routes.SUPPORTED_COUNTRY_CODES) --
# not a new stored field. Falls back to the raw code for anything
# unrecognized rather than guessing.
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


def _effective_period_label(order: Order, period_label: str | None) -> str | None:
    """EXACT DATE RANGE product (AM's passenger_car) has no period_code/label
    at all by design (see app.pricing.provider.get_duration_range) -- callers
    pass period_label=None in that case, since there's no period to resolve
    a label for. Never leave the period simply missing when we can still say
    something useful: derive "N дней" from the dates themselves, same
    "end - start" duration definition used everywhere else this rollout
    (see app.web.checkout_routes._parse_duration_range_dates and
    app.web.routes.get_summary's identical fallback for the customer-facing
    Summary screen). Never a fake period code -- just a label."""
    if not period_label and order.period_code is None and order.start_date and order.end_date:
        duration_days = (order.end_date - order.start_date).days
        return f"{duration_days} дней"
    return period_label


def _dates_line(order: Order) -> str | None:
    if order.start_date and order.end_date:
        return f"{order.start_date.strftime('%d.%m.%Y')} — {order.end_date.strftime('%d.%m.%Y')}"
    return None


def _amount_line(order: Order) -> str | None:
    if order.price_customer_minor is None:
        return None
    return f"{_format_rub(order.price_customer_minor // 100)} ₽"


def format_paid_order_message(order: Order, *, category_name: str | None, period_label: str | None) -> str:
    """Plain-text summary an operator needs to manually issue the policy —
    every field here is read from the existing Order/catalog data, nothing
    invented. Optional fields that are empty are simply omitted (never
    rendered as "Label: None" or "Label: "). engine_power/model_year/
    date_of_birth are the AM/TR-only Step 4 fields (see
    app.web.checkout_routes._requires_engine_power/_requires_model_year/
    _requires_date_of_birth) -- always None for GE, so those three lines
    simply never appear for a GE order; no country check needed here,
    same value-based _lines() omission every other optional field uses."""
    country_name = _COUNTRY_NAMES.get(order.country_code, order.country_code)
    amount = _amount_line(order)
    dates = _dates_line(order)
    effective_period_label = _effective_period_label(order, period_label)

    sections = [
        "\n".join(["💰 ОПЛАЧЕННЫЙ ЗАКАЗ", order.public_number]),
        "\n".join(
            _lines(
                ("Страна", country_name),
                ("Категория", category_name),
                ("Период", effective_period_label),
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
                ("Мощность двигателя", f"{order.engine_power} л.с." if order.engine_power else None),
                ("Год выпуска", order.model_year),
            )
        ),
        "\n".join(
            ["👤 СТРАХОВАТЕЛЬ", ""]
            + _lines(
                ("ФИО", order.full_name),
                ("Идентификационный номер", order.identification_number),
                ("Гражданство", order.citizenship),
                ("Дата рождения", order.date_of_birth.strftime("%d.%m.%Y") if order.date_of_birth else None),
            )
        ),
        "\n".join(["📞 КОНТАКТЫ", ""] + [f"{label}: {value}" for label, value in order.contact_rows]),
        "\n".join(["👤 СОБСТВЕННИК", ""] + _owner_lines(order)),
        "\n".join(["👤 ВОДИТЕЛЬ", ""] + _driver_lines(order)),
        "Статус: ОПЛАЧЕНО",
    ]
    return "\n\n".join(sections)


def format_new_order_message(order: Order, *, category_name: str | None, period_label: str | None) -> str:
    """Short notification sent the moment an Order is created (DATA_COMPLETED)
    -- see app.web.checkout_routes.post_policyholder, the only caller. Only
    the fields already safely available on the Order at this point; never
    the resume_token (see the module docstring) -- public_number is the
    operator-facing identifier everywhere in this module."""
    country_name = _COUNTRY_NAMES.get(order.country_code, order.country_code)
    effective_period_label = _effective_period_label(order, period_label)
    dates = _dates_line(order)
    if effective_period_label and dates:
        period_and_dates = f"{effective_period_label} ({dates})"
    else:
        period_and_dates = effective_period_label or dates
    contact = ", ".join(f"{label}: {value}" for label, value in order.contact_rows) or None

    lines = ["🆕 Новая заявка", "", order.public_number] + _lines(
        ("Страна", country_name),
        ("Транспорт", category_name),
        ("Период/даты", period_and_dates),
        ("Цена", _amount_line(order)),
        ("ФИО", order.full_name),
        ("Контакт", contact),
    )
    return "\n".join(lines)


def format_payment_claimed_message(order: Order) -> str:
    """Short notification sent the moment a customer clicks "Я оплатил"
    (AWAITING_PAYMENT -> PAYMENT_REVIEW) -- see
    app.web.routes.post_confirm_payment, the only caller. The most
    operationally important notification: it's what tells a human to go
    check their bank statement for this specific amount/order. Deliberately
    minimal -- just enough to find and match the incoming transfer."""
    lines = ["💳 Клиент сообщил об оплате", "", order.public_number] + _lines(
        ("Цена", _amount_line(order)),
        ("ФИО", order.full_name),
    )
    return "\n".join(lines)


def _notify_operator(
    *,
    api_id: int | None,
    api_hash: str | None,
    phone: str | None,
    session_path: Path,
    chat_id: int | str | None,
    order: Order,
    text: str,
) -> bool:
    """Shared best-effort send-and-log path every notify_operator_* function
    goes through -- the ONE place that actually talks to Telegram (via
    send_message/_send_via_telethon above). NEVER raises: returns True on
    success, False on any failure (including "not configured"), so a
    Telegram outage can never affect the order/payment state the caller is
    reporting on. Failures are logged (safe classification only, never the
    phone/api_hash)."""
    if not api_id or not api_hash or not phone or not chat_id:
        logger.warning("Telegram operator notification skipped: not configured (order %s)", order.public_number)
        return False
    try:
        send_message(api_id=api_id, api_hash=api_hash, session_path=session_path, chat_id=chat_id, text=text)
    except TelegramNotifyError as exc:
        logger.warning("Telegram operator notification failed for order %s: %s", order.public_number, exc)
        return False
    return True


def notify_operator_new_order(
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
    """Best-effort operator notification for a just-created Order
    (DATA_COMPLETED). Returns True on success, False on any failure -- NEVER
    raises. See app.web.checkout_routes.post_policyholder, the only caller:
    the Order is already committed before this runs, and this function's
    outcome must never affect checkout."""
    text = format_new_order_message(order, category_name=category_name, period_label=period_label)
    return _notify_operator(
        api_id=api_id, api_hash=api_hash, phone=phone, session_path=session_path, chat_id=chat_id, order=order, text=text
    )


def notify_operator_payment_claimed(
    *,
    api_id: int | None,
    api_hash: str | None,
    phone: str | None,
    session_path: Path,
    chat_id: int | str | None,
    order: Order,
) -> bool:
    """Best-effort operator notification for a just-claimed payment
    (AWAITING_PAYMENT -> PAYMENT_REVIEW). Returns True on success, False on
    any failure -- NEVER raises. See app.web.routes.post_confirm_payment,
    the only caller: idempotent by construction there (only called inside
    the same status-guarded branch that performs the transition itself, so
    a repeat POST/refresh that finds the order no longer AWAITING_PAYMENT
    never re-enters this at all -- see that function's own docstring)."""
    text = format_payment_claimed_message(order)
    return _notify_operator(
        api_id=api_id, api_hash=api_hash, phone=phone, session_path=session_path, chat_id=chat_id, order=order, text=text
    )


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
    text = format_paid_order_message(order, category_name=category_name, period_label=period_label)
    return _notify_operator(
        api_id=api_id, api_hash=api_hash, phone=phone, session_path=session_path, chat_id=chat_id, order=order, text=text
    )
