"""Customer side after the final review: creating the order (exactly once),
payment details, receipts, order status, policy resend, and explicitly
starting another purchase.

Order creation is idempotent end to end. The completed draft gets a
client_checkout_id BEFORE the order is created; the order row carries it
under a unique index. So a double tap, a replayed callback, or a restart
in the middle of creation always resolves to the SAME order: a second
attempt finds (or collides with) the existing row and just finishes the
remaining idempotent steps (attach documents, move to AWAITING_PAYMENT,
point the draft at the order).
"""

import logging
import sqlite3
import uuid

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.checkout import service as checkout_service
from app.notifications import bot_outbox
from app.orders import files as order_files
from app.orders.models import Order
from app.orders.payment import mark_awaiting_payment, order_event_properties, submit_payment_claim
from app.orders.repository import get_order_by_client_checkout_id
from app.orders.state_machine import OrderStatus
from app.telegram_bot import order_views, texts
from app.telegram_bot.context import Ctx
from app.telegram_bot.keyboards import FinalCb, OrderCb
from app.telegram_bot.steps import go, ready_for_order, reset_checkout, selection_complete, show

logger = logging.getLogger(__name__)

RECEIPT_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
# Telegram's own Bot API download ceiling; receipts are never downloaded by
# us (only forwarded by file id), this just rejects absurd uploads.
MAX_RECEIPT_BYTES = 20 * 1024 * 1024


def _order_event(ctx: Ctx, name: str, order: Order, **extra) -> None:
    ctx.event(name, **order_event_properties(order), **extra)


def attach_documents(ctx: Ctx, order: Order, draft: dict) -> None:
    """The checkout's vehicle document photos onto the order (idempotent:
    one row per Telegram file)."""
    for document in draft.get("document_files") or []:
        order_files.add_file(
            ctx.conn,
            order_id=order.id,
            kind=order_files.KIND_VEHICLE_DOCUMENT,
            bot_key=ctx.profile.bot_key,
            telegram_file_id=document["file_id"],
            telegram_file_unique_id=document["file_unique_id"],
            mime_type=document.get("mime_type"),
            file_size=document.get("file_size"),
            telegram_media_type="photo" if document.get("kind") == "photo" else "document",
        )


def _finalize(ctx: Ctx, order: Order, draft: dict) -> Order:
    """Every step here is idempotent, so running it again after a crash or
    a replay is harmless."""
    attach_documents(ctx, order, draft)
    order = mark_awaiting_payment(ctx.conn, order.id, note="telegram checkout confirmed").order
    # The draft now only points at the order (plus who the customer is and
    # where they came from) -- the business data lives on the order. A new
    # checkout generation, so nothing still in flight for the checkout can
    # write into the draft any more.
    reset_checkout(ctx, order_id=order.id)
    return order


def create_order_record(ctx: Ctx, draft: dict, checkout_id: str, *, operator: bool = False) -> tuple[Order | None, bool]:
    """(order, created_now) for this checkout id -- the ONE place a
    Telegram order row is created (customer and operator alike). The
    client_checkout_id's unique index makes a replay find the same row.

    operator=True: a manager issuing for a customer. The policyholder is
    ONLY the person in the draft's policyholder fields (from their
    documents); the manager stays the Telegram side of the order (actor and
    the chat that receives the PDF) and is never written into the
    policyholder's contacts."""
    order = get_order_by_client_checkout_id(ctx.conn, checkout_id)
    if order is not None:
        return order, False
    try:
        order = checkout_service.create_order_from_draft(
            ctx.conn,
            ctx.settings,
            session_id=ctx.session_id,
            policyholder={
                "full_name": draft["full_name"],
                "identification_number": draft["identification_number"],
                "citizenship": draft["citizenship"],
                # the bot's fixed contacts when configured, else the customer's own
                "contact_email": ctx.profile.customer_email or draft["contact_email"],
                "contact_phone": ctx.profile.customer_phone or draft["contact_phone"],
                # the customer's own Telegram -- never the manager's on an operator order
                "contact_telegram": (
                    f"@{draft['telegram_username']}" if draft.get("telegram_username") and not operator else None
                ),
                "contact_max": None,
                "contact_other": None,
            },
            channel="telegram",
            bot_key=ctx.profile.bot_key,
            telegram_user_id=ctx.user_id,
            telegram_chat_id=ctx.chat_id,
            telegram_username=draft.get("telegram_username"),
            acquisition_source=None if operator else draft.get("acquisition_source"),
            client_checkout_id=checkout_id,
            payment_mode="operator" if operator else None,
            created_by_telegram_user_id=ctx.user_id,
        )
        return order, True
    except sqlite3.IntegrityError:
        ctx.conn.rollback()
        return get_order_by_client_checkout_id(ctx.conn, checkout_id), False
    except checkout_service.OrderFromDraftError:
        return None, False


def create_or_resume_order(ctx: Ctx) -> tuple[Order | None, bool]:
    """(order, created_now). (None, False) when the draft can't be ordered."""
    draft = ctx.draft()
    existing = order_views.customer_order(ctx.conn, ctx.profile, ctx.user_id, draft.get("order_id"))
    if existing is not None:
        return existing, False

    checkout_id = draft.get("checkout_id")
    if not checkout_id:
        checkout_id = uuid.uuid4().hex
        draft = ctx.merge({"checkout_id": checkout_id})  # committed BEFORE the order exists

    order, created = create_order_record(ctx, draft, checkout_id)
    if order is None:
        return None, False
    return _finalize(ctx, order, ctx.draft()), created


async def on_review_confirmed(callback: CallbackQuery, state: FSMContext, ctx: Ctx):
    """"✅ Всё верно" on the consolidated review = the FINAL confirmation:
    re-validate, create ONE order and show the payment details right away
    -- no second "Проверьте заявку" screen. Something still missing or no
    longer valid -> that step instead (it leads back to the review). A
    repeated/queued tap finds the order in the draft and shows the same
    order (creation itself is idempotent, see create_or_resume_order)."""
    draft = ctx.draft()
    if not draft.get("order_id"):
        missing = checkout_service.vehicle_missing_fields(ctx.conn, draft)
        if missing:
            await callback.answer(texts.VEHICLE_INCOMPLETE.format(fields=texts.missing_fields_text(missing)), show_alert=True)
            await go(callback, state, ctx, "checkout_review")
            return
        if not draft.get("vehicle_confirmed") and selection_complete(ctx, draft):
            draft = ctx.merge({"vehicle_confirmed": True})  # this review showed the vehicle too
            ctx.event(
                "bot_vehicle_data_confirmed",
                data_entry_method=draft.get("data_entry_method"),
                identifier_type=draft.get("identifier_type"),
            )
        if not ready_for_order(ctx, draft):
            await go(callback, state, ctx, "resume")  # the first missing/invalid step; it returns to the review
            await callback.answer()
            return
    order, created = create_or_resume_order(ctx)
    if order is None:
        await go(callback, state, ctx, "resume", notice=texts.ORDER_CREATE_FAILED)
        await callback.answer()
        return
    if created:
        _order_event(ctx, "bot_order_created", order)
    await go(callback, state, ctx, "order")
    await callback.answer()


async def on_order_action(callback: CallbackQuery, callback_data: OrderCb, state: FSMContext, ctx: Ctx):
    order = order_views.customer_order(ctx.conn, ctx.profile, ctx.user_id, callback_data.order_id)
    if order is None:
        await go(callback, state, ctx, "resume")
        await callback.answer()
        return
    action = callback_data.action
    if action == "resume":
        # "↩️ Вернуться к заказу" (from /start): the draft points at the order
        # again -- any unfinished NEW checkout is dropped -- and the ordinary
        # order screen (payment / receipt / status) takes over.
        reset_checkout(ctx, floor_message_id=callback.message.message_id if callback.message else None, order_id=order.id)
        await state.clear()
        await go(callback, state, ctx, "order")
        await callback.answer()
        return
    if action == "receipt":
        if order.status in order_views.RECEIPT_STATUSES:
            await callback.bot.send_message(ctx.chat_id, texts.RECEIPT_PROMPT)
        else:
            await go(callback, state, ctx, "order")
    elif action == "details":
        await show(callback, *(_order_screen(ctx, order)))
    elif action == "resend_policy":
        policy = order_files.latest_file(ctx.conn, order.id, order_files.KIND_POLICY)
        if policy is not None and order.status in order_views.POLICY_DELIVERED_STATUSES:
            # Always the chat stored on the order.
            await callback.bot.send_document(
                order.telegram_chat_id, policy.telegram_file_id, caption=texts.POLICY_CAPTION.format(number=order.public_number)
            )
        else:
            await go(callback, state, ctx, "order")
    elif action == "new":
        if order.status in order_views.UNFINISHED_STATUSES:
            await callback.answer(texts.NEW_PURCHASE_NOT_ALLOWED, show_alert=True)
            await go(callback, state, ctx, "order")
            return
        if ctx.draft().get("order_id") == order.id:
            # Start a fresh checkout; the previous order (and the customer's
            # source) stay exactly as they are.
            reset_checkout(ctx, floor_message_id=callback.message.message_id if callback.message else None)
        await go(callback, state, ctx, "categories")
    await callback.answer()


def _order_screen(ctx: Ctx, order: Order):
    text, details_shown = order_views.order_text(ctx.settings, order)
    if order.status == OrderStatus.AWAITING_PAYMENT.value:
        _order_event(ctx, "bot_payment_details_shown", order, available=details_shown)
    return text, order_views.order_keyboard(ctx.conn, order)


async def _receipt(message: Message, state: FSMContext, ctx: Ctx, *, file_id, file_unique_id, mime_type, file_size, media_type):
    order = order_views.customer_order(ctx.conn, ctx.profile, ctx.user_id, ctx.draft().get("order_id"))
    if order is None:
        raise SkipHandler()  # not a receipt -- let the normal flow handle it
    number = order.public_number
    if order.status not in order_views.RECEIPT_STATUSES:
        # Already paid (or later): a receipt never changes the payment state.
        text, keyboard = _order_screen(ctx, order)
        await message.answer(f"{texts.RECEIPT_NOT_NEEDED.format(number=number)}\n\n{text}", reply_markup=keyboard)
        return
    if mime_type not in RECEIPT_MIME_TYPES:
        await message.answer(texts.RECEIPT_UNSUPPORTED)
        return
    if file_size is not None and file_size > MAX_RECEIPT_BYTES:
        await message.answer(texts.RECEIPT_TOO_BIG)
        return

    # Store the receipt FIRST, on its own commit: whatever happens next
    # (state change, manager notification), it is never lost.
    row_id = order_files.add_file(
        ctx.conn,
        order_id=order.id,
        kind=order_files.KIND_PAYMENT_RECEIPT,
        bot_key=ctx.profile.bot_key,
        telegram_file_id=file_id,
        telegram_file_unique_id=file_unique_id,
        mime_type=mime_type,
        file_size=file_size,
        telegram_media_type=media_type,
    )
    if row_id is None:
        await message.answer(texts.RECEIPT_DUPLICATE)
        return
    _order_event(ctx, "bot_receipt_uploaded", order, media=media_type, pdf=mime_type == "application/pdf")

    claim = submit_payment_claim(
        ctx.conn,
        order.id,
        note="telegram receipt uploaded",
        manager_ids=ctx.manager_ids,
        file_row_ids=[f.id for f in order_files.list_files(ctx.conn, order.id)],
    )
    if claim.changed:
        _order_event(ctx, "bot_payment_review_started", claim.order)
        await message.answer(texts.RECEIPT_RECEIVED.format(number=number), reply_markup=order_views.order_keyboard(ctx.conn, claim.order))
        await ctx.deliver(claim.outbox_job_ids)
        return

    # Already under review: forward this additional receipt to the managers
    # (same review round; no second transition, no second order).
    review_round = bot_outbox.current_review_round(ctx.conn, order.id)
    job_ids = []
    if claim.order.status == OrderStatus.PAYMENT_REVIEW.value and review_round is not None:
        for manager_id in sorted(ctx.manager_ids):
            job_id = bot_outbox.enqueue_manager_file(
                ctx.conn, claim.order, review_round=review_round, manager_id=manager_id, file_row_id=row_id, extra=True
            )
            if job_id:
                job_ids.append(job_id)
        ctx.conn.commit()
    await message.answer(texts.RECEIPT_EXTRA_RECEIVED.format(number=number), reply_markup=order_views.order_keyboard(ctx.conn, claim.order))
    await ctx.deliver(job_ids)


async def on_receipt_photo(message: Message, state: FSMContext, ctx: Ctx):
    photo = message.photo[-1]
    await _receipt(
        message, state, ctx, file_id=photo.file_id, file_unique_id=photo.file_unique_id,
        mime_type="image/jpeg", file_size=photo.file_size, media_type="photo",
    )


async def on_receipt_document(message: Message, state: FSMContext, ctx: Ctx):
    document = message.document
    await _receipt(
        message, state, ctx, file_id=document.file_id, file_unique_id=document.file_unique_id,
        mime_type=document.mime_type, file_size=document.file_size, media_type="document",
    )


def register(router: Router) -> None:
    router.callback_query.register(on_review_confirmed, FinalCb.filter(F.action.in_({"confirm", "continue"})))
    router.callback_query.register(on_order_action, OrderCb.filter())
    router.message.register(on_receipt_photo, F.photo)
    router.message.register(on_receipt_document, F.document)
