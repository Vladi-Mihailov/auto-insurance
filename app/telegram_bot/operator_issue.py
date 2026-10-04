"""tpl.ge policy issuance from the bot -- ONE machinery for every order:

A) operator order: a manager issues for a customer and skips OUR customer
   payment (below);
B) normal customer order: the customer paid us (Sber), uploaded a receipt,
   a manager pressed "✅ Оплата поступила" (app.telegram_bot.managers) --
   right after that confirmation is committed and acknowledged, the SAME
   start_tpl_issuance() runs for the order.

Both differ only in how OUR order reached PAID and in who gets the PDF:
the order's own telegram_chat_id -- the customer for (B), the issuing
manager for (A). From PAID on: start_tpl_issuance() (TPL application + BOG
link), the manager pays TPL with the company card, deliver() (policy +
PDF). The manual PDF upload stays only as an explicit fallback.

--- (A) Operator issuance: a manager/owner issues a TPL policy FOR A
CUSTOMER directly from the bot.

The data is collected by the SAME wizard customers use (category, period,
date, the customer's documents / manual entry, OCR, the consolidated
review). On that review staff see "🛡 Оформить страховку" next to "💳 Создать
заказ с оплатой" (the ordinary customer route). "🛡" -> ONE explicit
"🛡 Оформить полис?" confirmation (it triggers a real, external insurer
action) -> "✅ Оформить полис":

1. a normal, auditable order is created (the same create_order_record as a
   customer order) with payment_mode 'operator' and the manager as the
   Telegram actor/recipient; the policyholder is ONLY the person from the
   documents (never the manager, never a fake Telegram user);
2. DATA_COMPLETED -> PAID via app.orders.payment.mark_operator_issuance,
   whose history note says that customer payment collection was bypassed
   (no Sber screen, no receipt, no payment check);
3. the SAME insurer integration the web admin uses
   (app.integrations.tpl_ge.service.issue_tpl_policy -> TPL application +
   Bank of Georgia payment link), with the same mapping (category /
   catalog external ids incl. "Other", VIN/chassis, period, start date,
   policyholder, citizenship, fixed contacts);
4. TPL itself is paid by the company card in the manager's own browser
   (3-D Secure/SMS is always a human, exactly as on the web), then
   "✅ Оплата TPL завершена" -> report_operator_paid +
   retrieve_issued_policy_with_retry -> the policy PDF is downloaded and
   sent to the manager's chat (the order's telegram_chat_id), stored as the
   order's policy file, order -> POLICY_READY.

Idempotency (a double tap must never create two policies):
- the order's client_checkout_id is "op:<checkout generation>" -- a repeated
  or replayed "✅ Оформить полис" finds the SAME order;
- per order, at most ONE POST /api/policies ever (tpl_ge.service: the
  request is claimed atomically before it is sent; an unknown outcome is
  never re-sent, only probed);
- a per-order asyncio lock serializes this process's issuance steps, and
  every step re-reads the DB;
- nothing critical lives in the FSM: after a restart the order, its
  issuance row and every button keep working from the DB.

Authorization: every press re-checks the ACTIVE staff role in the DB.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import uuid
from collections import defaultdict
from datetime import date

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.checkout import service as checkout_service
from app.checkout.rules import draft_country_code
from app.integrations.tpl_ge import repository as tpl_repo
from app.integrations.tpl_ge import service as tpl_service
from app.integrations.tpl_ge.errors import (
    ApplicationOutcomeUnknownError,
    IssuanceInProgressError,
    MissingRequiredDataError,
    ProductNotFoundError,
    TplApplicationError,
    TplIssuanceError,
    VisitorIdNotConfiguredError,
)
from app.integrations.tpl_ge.models import IssuanceStatus
from app.notifications import bot_outbox
from app.orders import files as order_files
from app.orders.models import Order
from app.orders.payment import mark_operator_issuance, order_event_properties
from app.orders.repository import get_order_by_client_checkout_id, get_order_by_id, transition_if
from app.orders.state_machine import OrderStatus
from app.pricing.provider import get_period
from app.settings import Settings
from app.telegram_bot import staff, texts
from app.telegram_bot.context import Ctx
from app.telegram_bot.keyboards import IssueCb, MgrCb, NavCb, OpCb, StaffCb
from app.telegram_bot.orders import attach_documents, create_order_record
from app.telegram_bot.profile import BotProfile
from app.telegram_bot.steps import (
    _fmt,
    category_label,
    checkout_gen,
    current_price_rub,
    go,
    ready_for_order,
    reset_checkout,
    selection_complete,
    show,
    vehicle_make_display,
    vehicle_model_display,
)

logger = logging.getLogger(__name__)

# The draft fields an issuance is made of -- the confirmation's fingerprint.
_FINGERPRINT_KEYS = (
    "vehicle_category_code", "period_code", "start_date", "end_date", "price_customer_minor",
    "registration_number", "identifier_type", "identifier", "manufacturer_id", "model_id",
    "vehicle_make_text", "vehicle_model_text", "full_name", "identification_number", "citizenship",
    "contact_email", "contact_phone",
)
_order_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def operator_checkout_id(gen: str) -> str:
    return f"op:{gen}"


def fingerprint(draft: dict) -> str:
    data = json.dumps({key: draft.get(key) for key in _FINGERPRINT_KEYS}, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:10]


def _markup(rows) -> InlineKeyboardMarkup:
    out = []
    for row in rows:
        built = []
        for text, target in row:
            if isinstance(target, str):  # a URL button
                built.append(InlineKeyboardButton(text=text, url=target))
            else:
                built.append(InlineKeyboardButton(text=text, callback_data=target.pack()))
        if built:
            out.append(built)
    return InlineKeyboardMarkup(inline_keyboard=out)


# ---------------------------------------------------------------- screens


def confirmation_text(ctx: Ctx, draft: dict, price_rub: int | None) -> str:
    period = get_period(ctx.settings, draft_country_code(draft), draft["vehicle_category_code"], draft["period_code"])
    identifier_label = "VIN" if draft.get("identifier_type") == "vin" else "Шасси"
    lines = [
        texts.OP_CONFIRM_TITLE,
        "",
        f"Страхователь: {draft.get('full_name')}",
        f"Паспорт: {draft.get('identification_number')}",
        f"Гражданство: {texts.citizenship_label(draft.get('citizenship'))}",
        f"Автомобиль: {vehicle_make_display(ctx, draft)} {vehicle_model_display(ctx, draft)}",
        f"Госномер: {draft.get('registration_number')}",
        f"{identifier_label}: {draft.get('identifier')}",
        f"Категория: {category_label(ctx, draft['vehicle_category_code'])}",
        f"Период: {period.label if period else draft['period_code']}",
        f"Начало: {_fmt(date.fromisoformat(draft['start_date']))}",
    ]
    if price_rub is not None:
        lines.append(f"Стоимость: {texts.format_rub(price_rub)} ₽")
    lines += ["", texts.OP_CONFIRM_FOOTER]
    return "\n".join(lines)


def _summary(settings: Settings, order: Order) -> str:
    period = get_period(settings, order.country_code, order.vehicle_category_code, order.period_code) if order.period_code else None
    make = order.vehicle_make_document or order.vehicle_make or "—"
    model = order.vehicle_model_document or order.vehicle_model or "—"
    first = texts.OP_ORDER_LINE if order.is_operator_order else texts.ISSUE_ORDER_LINE_CUSTOMER
    lines = [
        first.format(number=order.public_number),
        f"Страхователь: {order.full_name or '—'}",
        f"Автомобиль: {make} {model} · {order.display_registration_number or '—'}",
        f"Период: {period.label if period else order.period_code} · начало {order.start_date:%d.%m.%Y}" if order.start_date else "",
        f"Стоимость: {texts.format_rub(order.price_customer_minor // 100)} ₽" if order.price_customer_minor is not None else "",
    ]
    return "\n".join(line for line in lines if line)


def classify(exc: BaseException) -> str:
    if isinstance(exc, VisitorIdNotConfiguredError):
        return "not_configured"
    if isinstance(exc, (MissingRequiredDataError, ProductNotFoundError)):
        return "validation"
    if isinstance(exc, TplApplicationError):
        return "rejected"
    if isinstance(exc, ApplicationOutcomeUnknownError):
        return "unknown"
    if isinstance(exc, IssuanceInProgressError):
        return "in_progress"
    return "temporary"


_ISSUABLE = (OrderStatus.PAID.value, OrderStatus.PROCESSING.value)


def status_view(conn: sqlite3.Connection, settings: Settings, order: Order, *, kind: str | None = None, error: str | None = None):
    """(text, markup) for an order's tpl.ge issuance -- always derived from the DB."""
    summary = _summary(settings, order)
    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    operator = order.is_operator_order
    if operator:
        back = [(texts.BTN_STAFF_TO_ORDERS, StaffCb(a="orders"))]
    else:
        back = [(texts.BTN_TO_ORDER, StaffCb(a="co", i=order.id))]
    op = lambda a: OpCb(a=a, order_id=order.id)  # noqa: E731
    # A customer order: the manual PDF upload stays available as an explicit fallback.
    manual = [] if operator else [(texts.BTN_MGR_SEND_POLICY, MgrCb(action="policy", order_id=order.id))]
    if order.status == OrderStatus.CANCELLED.value:
        return texts.OP_CANCELLED.format(number=order.public_number), _markup([back])
    if not operator and order.status not in (*_ISSUABLE, OrderStatus.POLICY_READY.value, OrderStatus.COMPLETED.value):
        return texts.ISSUE_NOT_PAID.format(summary=summary), _markup([back])
    if kind == "not_configured":
        return texts.ISSUE_NOT_CONFIGURED.format(summary=summary), _markup([[(texts.BTN_OP_RETRY, op("retry"))], manual, back])
    if kind == "in_progress":
        return f"{texts.OP_IN_PROGRESS}\n\n{summary}", _markup([[(texts.BTN_OP_CHECK, op("status"))], back])
    if issuance is not None and issuance.is_policy_retrieved:
        if issuance.is_sent_to_operator:
            done = texts.OP_POLICY_DONE if operator else texts.ISSUE_POLICY_DONE_CUSTOMER
            return (
                done.format(policy=issuance.policy_number, summary=summary),
                _markup([[(texts.BTN_OP_RESEND, op("resend"))], back]),
            )
        return (
            texts.OP_POLICY_NOT_SENT.format(policy=issuance.policy_number, summary=summary),
            _markup([[(texts.BTN_OP_GET_POLICY, op("paid"))], back]),
        )
    if issuance is not None and issuance.is_operator_reported_paid:
        return texts.OP_REPORTED_PAID.format(summary=summary), _markup([[(texts.BTN_OP_GET_POLICY, op("paid"))], back])
    if issuance is not None and issuance.is_bog_link_ready:
        price = f"{issuance.tpl_purchase_price_gel:.2f}" if issuance.tpl_purchase_price_gel is not None else "—"
        rows = [
            [(texts.BTN_OP_PAY_TPL, issuance.bog_payment_url)],
            [(texts.BTN_OP_TPL_PAID, op("paid"))],
            [(texts.BTN_OP_NEW_LINK, op("link"))],
            back,
        ]
        return texts.OP_BOG_READY.format(summary=summary, price_gel=price), _markup(rows)
    if issuance is not None and issuance.is_application_requested:
        return texts.OP_REQUESTED.format(summary=summary), _markup([[(texts.BTN_OP_CHECK, op("retry"))], back])
    if issuance is None and kind is None and not operator:
        # a confirmed customer payment, issuance not started yet (e.g. confirmed
        # before this flow existed, or on the web): start it here
        return texts.ISSUE_CUSTOMER_PAID.format(summary=summary), _markup([[(texts.BTN_TPL_START, op("start"))], manual, back])
    last_error = error or (issuance.last_error if issuance else None) or "—"
    if issuance is not None and issuance.application_already_created:
        # created at TPL, only the payment link is missing: get a (new) one
        return texts.OP_FAILED_TEMPORARY.format(summary=summary, error=last_error), _markup([[(texts.BTN_OP_NEW_LINK, op("link"))], back])
    if kind is None:
        kind = "rejected" if issuance is not None and issuance.is_failed and "rejected" in last_error else (
            "validation" if issuance is not None and issuance.is_failed else "temporary"
        )
    template = {
        "validation": texts.OP_FAILED_VALIDATION,
        "rejected": texts.OP_FAILED_REJECTED,
    }.get(kind, texts.OP_FAILED_TEMPORARY)
    fix = [(texts.BTN_OP_FIX, NavCb(to="checkout_review"))] if operator else manual
    rows = [[(texts.BTN_OP_RETRY, op("retry"))], fix, back]
    text = template.format(summary=summary, error=last_error)
    if not operator:
        text = text.replace(texts.OP_FIX_HINT, texts.ISSUE_FIX_HINT_CUSTOMER)
    return text, _markup(rows)


# ------------------------------------------------------------- the work


def _reload(conn: sqlite3.Connection, order: Order) -> Order:
    return get_order_by_id(conn, order.id)


async def start_tpl_issuance(conn: sqlite3.Connection, settings: Settings, order: Order, *, actor: str, refresh_link: bool = False):
    """THE issuance step for every order (operator or customer): move it as
    far as it safely goes without a human -- the TPL application + BOG
    payment link, only if no application exists yet (or, refresh_link, a
    new link for an existing one that hasn't been paid). An operator order
    is first moved to PAID (payment collection bypassed); a customer order
    must already be PAID by a confirmed payment -- nothing here ever
    confirms a customer payment. Returns (order, error kind or None, error
    text or None)."""
    async with _order_locks[order.id]:
        order = _reload(conn, order)
        if order.status == OrderStatus.DATA_COMPLETED.value and order.is_operator_order:
            mark_operator_issuance(conn, order.id, actor=actor)
            order = _reload(conn, order)
        if order.status not in _ISSUABLE:
            return order, None, None
        issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
        needs_application = issuance is None or not issuance.application_already_created
        can_refresh = issuance is not None and issuance.issuance_status in (
            IssuanceStatus.APPLICATION_CREATED.value, IssuanceStatus.BOG_LINK_READY.value,
        )
        if not (needs_application or (refresh_link and can_refresh)):
            return order, None, None  # nothing to do: never a second application, never a link after payment
        if not settings.tpl_ge.static_visitor_id:
            # Fails safe BEFORE anything: no issuance row, no tpl.ge call at all.
            logger.warning("tpl.ge issuance for order %s not started: TPL_GE_STATIC_VISITOR_ID is not configured", order.public_number)
            return order, "not_configured", None
        try:
            await asyncio.to_thread(tpl_service.issue_tpl_policy, conn, order, settings)
        except TplIssuanceError as exc:
            logger.warning("Operator issuance for order %s: %s", order.public_number, type(exc).__name__)
            return _reload(conn, order), classify(exc), str(exc)
        except Exception as exc:  # noqa: BLE001 -- e.g. a network error while READING tpl.ge (nothing sent)
            logger.warning("Operator issuance for order %s failed: %s", order.public_number, type(exc).__name__)
            if tpl_repo.get_issuance_by_order_id(conn, order.id) is not None:
                tpl_repo.record_error(conn, order.id, error_message=f"tpl.ge unavailable ({type(exc).__name__})")
            return _reload(conn, order), "temporary", f"tpl.ge недоступен ({type(exc).__name__})"
        return _reload(conn, order), None, None


async def deliver(bot: Bot, conn: sqlite3.Connection, settings: Settings, order: Order, *, outbox=None):
    """"✅ Оплата TPL завершена" / "🔄 Получить полис": the web admin's exact
    sequence (report_operator_paid -> bounded retrieval), then the PDF -- at
    most once -- to the ORDER'S OWN chat: the customer who owns a normal
    order, the issuing manager for an operator order (never anyone else)."""
    async with _order_locks[order.id]:
        order = _reload(conn, order)
        try:
            issuance = tpl_service.report_operator_paid(conn, order)
        except TplIssuanceError as exc:
            return _reload(conn, order), classify(exc), str(exc)
        if not issuance.is_policy_retrieved:
            issuance = await asyncio.to_thread(tpl_service.retrieve_issued_policy_with_retry, conn, order, settings)
        if not issuance.is_policy_retrieved or issuance.is_sent_to_operator:
            return _reload(conn, order), None, None
        if not issuance.policy_document_url:
            tpl_repo.record_error(conn, order.id, error_message="Policy document URL missing -- cannot deliver PDF")
            return _reload(conn, order), "temporary", "нет ссылки на PDF полиса"
        try:
            pdf = await asyncio.to_thread(tpl_service.download_policy_pdf, issuance.policy_document_url)
        except Exception as exc:  # noqa: BLE001
            tpl_repo.record_error(conn, order.id, error_message=f"Policy PDF download failed: {type(exc).__name__}")
            return _reload(conn, order), "temporary", "PDF полиса не скачался"
        if order.is_operator_order:
            caption = texts.OP_POLICY_CAPTION.format(policy=issuance.policy_number, number=order.public_number, name=order.full_name)
        else:
            caption = texts.POLICY_CAPTION.format(number=order.public_number)  # the customer's usual "Страховка готова"
        try:
            sent = await bot.send_document(
                order.telegram_chat_id,
                BufferedInputFile(pdf, filename=f"policy-{issuance.policy_number}.pdf"),
                caption=caption,
            )
        except TelegramAPIError as exc:
            tpl_repo.record_error(conn, order.id, error_message=f"Telegram PDF delivery failed: {type(exc).__name__}")
            return _reload(conn, order), "temporary", "PDF не удалось отправить в Telegram"
        finally:
            del pdf
        document = getattr(sent, "document", None)
        if document is not None:
            # kept as the order's policy file (re-sendable by file id, like a manager-uploaded PDF)
            order_files.add_file(
                conn, order_id=order.id, kind=order_files.KIND_POLICY, bot_key=order.bot_key,
                telegram_file_id=document.file_id, telegram_file_unique_id=document.file_unique_id,
                mime_type="application/pdf", file_size=document.file_size, telegram_media_type="document",
            )
        tpl_repo.mark_policy_sent_to_operator(conn, order.id)
        recipient = "the issuing manager" if order.is_operator_order else "the customer"
        history_id = transition_if(
            conn, order.id, OrderStatus.PROCESSING, OrderStatus.POLICY_READY, note=f"tpl.ge policy PDF delivered to {recipient}",
            commit=False,
        )
        jobs = []
        if history_id is not None:
            jobs = bot_outbox.enqueue_card_updates(conn, _reload(conn, order), history_id=history_id)  # managers' cards
        conn.commit()
        if outbox is not None and jobs:
            await outbox.process(bot, conn, job_ids=jobs)
        logger.info("tpl.ge policy delivered for order %s", order.public_number)
        return _reload(conn, order), None, None


async def after_payment_confirmed(bot: Bot, conn: sqlite3.Connection, settings: Settings, order: Order, *, manager_chat_id: int, actor: str) -> None:
    """Called by the manager's "✅ Оплата поступила" AFTER the confirmation
    is committed and the press acknowledged: start the SAME tpl.ge issuance
    and show the manager where it stands. Never raises -- the payment
    confirmation stands whatever tpl.ge does; the manager can always resume
    from the order card ("🛡 Оформить в tpl.ge")."""
    try:
        order, kind, error = await start_tpl_issuance(conn, settings, order, actor=actor)
        text, markup = status_view(conn, settings, order, kind=kind, error=error)
        await bot.send_message(manager_chat_id, text, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tpl.ge issuance after payment confirmation of order %s: %s", order.public_number, type(exc).__name__)


# --------------------------------------------------------------- handlers


async def _answer(callback: CallbackQuery, *args, **kwargs) -> None:
    try:
        await callback.answer(*args, **kwargs)
    except TelegramBadRequest:
        pass


def _can_supersede(conn: sqlite3.Connection, order: Order) -> bool:
    """A previous attempt for this checkout that created NOTHING at TPL
    (no issuance, or it never got past PENDING/FAILED) -- safe to replace
    with an order made of the (corrected) data."""
    if order.status not in (OrderStatus.DATA_COMPLETED.value, OrderStatus.PAID.value):
        return False
    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    return issuance is None or issuance.issuance_status in (IssuanceStatus.PENDING.value, IssuanceStatus.FAILED.value)


async def on_issue(callback: CallbackQuery, callback_data: IssueCb, state: FSMContext, ctx: Ctx):
    if not staff.is_staff(ctx.conn, ctx.profile.bot_key, ctx.user_id):
        logger.warning("Rejected operator issuance from a non-staff user")
        await _answer(callback, texts.MGR_NO_ACCESS, show_alert=True)
        return
    action, g = callback_data.a, callback_data.g
    actor = f"telegram manager {ctx.user_id}"

    if action == "go" and g:
        existing = get_order_by_client_checkout_id(ctx.conn, operator_checkout_id(g))
        if existing is not None:
            # A repeat / replayed / restarted "✅ Оформить полис": the SAME order.
            await _answer(callback)
            existing, kind, error = await start_tpl_issuance(ctx.conn, ctx.settings, existing, actor=actor)
            await _show_status(callback, ctx, existing, kind, error, gen=g)
            return

    draft = ctx.draft()
    if draft.get("order_id"):
        await go(callback, state, ctx, "order")
        await _answer(callback)
        return
    gen = checkout_gen(draft)
    if g and g != gen:
        await go(callback, state, ctx, "resume")  # a button of an earlier checkout
        await _answer(callback)
        return
    missing = checkout_service.vehicle_missing_fields(ctx.conn, draft)
    if missing:
        await _answer(callback, texts.VEHICLE_INCOMPLETE.format(fields=texts.missing_fields_text(missing)), show_alert=True)
        await go(callback, state, ctx, "checkout_review")
        return
    if not draft.get("vehicle_confirmed") and selection_complete(ctx, draft):
        draft = ctx.merge({"vehicle_confirmed": True})  # the review showed the vehicle
    if not ready_for_order(ctx, draft):
        await go(callback, state, ctx, "resume")
        await _answer(callback)
        return
    if not gen:
        gen = uuid.uuid4().hex[:12]
        draft = ctx.merge({"checkout_gen": gen})

    if action == "ask":
        previous = get_order_by_client_checkout_id(ctx.conn, operator_checkout_id(gen))
        if previous is not None:
            if not _can_supersede(ctx.conn, previous):
                await _answer(callback)
                await _show_status(callback, ctx, previous, None, None, gen=gen)
                return
            # nothing exists at TPL for it: replace it with the corrected data
            transition_if(
                ctx.conn, previous.id, OrderStatus(previous.status), OrderStatus.CANCELLED,
                note="operator issuance superseded by a corrected attempt (nothing was created at TPL)",
            )
            gen = uuid.uuid4().hex[:12]
            draft = ctx.merge({"checkout_gen": gen})
        price_rub, _ = current_price_rub(ctx)
        draft = ctx.draft()
        rows = [
            [(texts.BTN_OP_ISSUE_GO, IssueCb(a="go", g=gen, h=fingerprint(draft)))],
            [(texts.BTN_OP_BACK, NavCb(to="checkout_review"))],
        ]
        await show(callback, confirmation_text(ctx, draft, price_rub), _markup(rows))
        await _answer(callback)
        return

    if action != "go":
        await _answer(callback)
        return
    price_rub, _ = current_price_rub(ctx)  # the price is re-read like any order creation
    draft = ctx.draft()
    if callback_data.h != fingerprint(draft):
        rows = [
            [(texts.BTN_OP_ISSUE_GO, IssueCb(a="go", g=gen, h=fingerprint(draft)))],
            [(texts.BTN_OP_BACK, NavCb(to="checkout_review"))],
        ]
        await show(callback, f"{texts.OP_CHANGED}\n\n{confirmation_text(ctx, draft, price_rub)}", _markup(rows))
        await _answer(callback)
        return

    order, created = create_order_record(ctx, draft, operator_checkout_id(gen), operator=True)
    if order is None:
        await go(callback, state, ctx, "resume", notice=texts.ORDER_CREATE_FAILED)
        await _answer(callback)
        return
    attach_documents(ctx, order, draft)
    if created:
        ctx.event("bot_operator_order_created", **order_event_properties(order))
    await _answer(callback, "⏳")  # before the (slow) insurer calls
    order, kind, error = await start_tpl_issuance(ctx.conn, ctx.settings, order, actor=actor)
    await _show_status(callback, ctx, order, kind, error, gen=gen)


async def _show_status(callback: CallbackQuery, ctx: Ctx, order: Order, kind, error, *, gen: str) -> None:
    issuance = tpl_repo.get_issuance_by_order_id(ctx.conn, order.id)
    if issuance is not None and (issuance.application_already_created or issuance.is_application_requested):
        # An application exists (or may): this checkout is done -- the draft
        # starts fresh, the order holds the data. Never offered again.
        draft = ctx.draft()
        if checkout_gen(draft) == gen and not draft.get("order_id"):
            reset_checkout(ctx, floor_message_id=callback.message.message_id if callback.message else None)
    text, markup = status_view(ctx.conn, ctx.settings, order, kind=kind, error=error)
    await show(callback, text, markup)


async def on_operator_order(
    callback: CallbackQuery, callback_data: OpCb, conn: sqlite3.Connection, settings: Settings, profile: BotProfile, bot: Bot,
    outbox=None,
):
    """Buttons on an order's tpl.ge issuance screen (also reachable from the
    order card / "📋 Заказы"). Any active staff member; the order must be
    this bot's Telegram order (operator, or a customer order)."""
    user_id = callback.from_user.id
    if not staff.is_staff(conn, profile.bot_key, user_id):
        logger.warning("Rejected operator order action %r from a non-staff user", callback_data.a)
        await _answer(callback, texts.MGR_NO_ACCESS, show_alert=True)
        return
    order = get_order_by_id(conn, callback_data.order_id)
    if order is None or order.channel != "telegram" or order.bot_key != profile.bot_key or order.telegram_chat_id is None:
        await _answer(callback, texts.MGR_ORDER_NOT_FOUND, show_alert=True)
        return
    action, kind, error = callback_data.a, None, None
    actor = f"telegram manager {user_id}"
    if not order.is_operator_order and not settings.telegram_bot.tpl_auto_issuance and action != "resend":
        # Automatic tpl.ge issuance of CUSTOMER orders is off: an old
        # "🛡 Оформить в tpl.ge" / "💳 Оплатить" / "🔄 Новая ссылка" button
        # never reaches tpl.ge -- the manager gets the order card (manual upload).
        from app.telegram_bot.managers import card_keyboard, card_text

        await _answer(callback, texts.MGR_TPL_AUTO_DISABLED, show_alert=True)
        text, markup = card_text(conn, settings, order), card_keyboard(conn, order)
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                await bot.send_message(user_id, text, reply_markup=markup)
        return
    if action in ("start", "retry", "link"):
        await _answer(callback, "⏳")
        order, kind, error = await start_tpl_issuance(conn, settings, order, actor=actor, refresh_link=action == "link")
    elif action == "paid":
        await _answer(callback, "⏳")
        order, kind, error = await deliver(bot, conn, settings, order, outbox=outbox)
    elif action == "resend":
        policy = order_files.latest_file(conn, order.id, order_files.KIND_POLICY)
        issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
        if policy is None or issuance is None:
            await _answer(callback, texts.MGR_ALREADY_DONE, show_alert=True)
        else:
            await _answer(callback)
            caption = (
                texts.OP_POLICY_CAPTION.format(policy=issuance.policy_number, number=order.public_number, name=order.full_name)
                if order.is_operator_order else texts.POLICY_CAPTION.format(number=order.public_number)
            )
            await bot.send_document(order.telegram_chat_id, policy.telegram_file_id, caption=caption)  # the order's own chat
        return
    else:
        await _answer(callback)
    text, markup = status_view(conn, settings, order, kind=kind, error=error)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            await bot.send_message(user_id, text, reply_markup=markup)


def register_customer_side(router: Router) -> None:
    """IssueCb needs the customer context (the manager's own draft)."""
    router.callback_query.register(on_issue, IssueCb.filter())


def register_staff_side(router: Router) -> None:
    router.callback_query.register(on_operator_order, OpCb.filter())
