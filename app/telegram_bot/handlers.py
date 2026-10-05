"""Customer conversation: /start, category, period, start date, the
data-entry-method choice, global navigation, fallbacks and the error
handler. Vehicle, document and policyholder input live in their own modules
(app.telegram_bot.vehicle / documents / policyholder); screens, step order
and draft-based gating live in app.telegram_bot.steps.

Handlers only translate between Telegram and app.checkout: every choice is
validated/stored by app.checkout.service into the customer's persisted
draft, and every screen is re-derived from that draft.
"""

import logging
import traceback
from datetime import date, timedelta
from pathlib import Path

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.enums import ChatType
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from app.checkout import service as checkout_service
from app.checkout.rules import auto_assigns_start_date, draft_country_code
from app.dates.rules import today_in_georgia
from app.telegram_bot import documents, operator_issue, order_views, orders, policyholder, staff, texts, vehicle
from app.telegram_bot.context import Ctx
from app.telegram_bot.keyboards import (
    CategoryCb,
    DateCb,
    MenuCb,
    MethodCb,
    NavCb,
    NewCheckoutCb,
    PeriodCb,
    date_keyboard,
)
from app.orders.repository import list_telegram_orders
from app.telegram_bot.order_views import UNFINISHED_STATUSES
from app.telegram_bot.sessions import record_start
from app.telegram_bot.steps import (
    NAV_TARGETS,
    RETURN_TARGETS,
    Flow,
    View,
    checkout_gen,
    intro_view,
    categories_view,
    go,
    offered_categories,
    parse_start_date_input,
    periods_view,
    policyholder_complete,
    reset_checkout,
    resume_step,
    selection_complete,
    start_date_valid,
    show,
    vehicle_confirmed,
)

logger = logging.getLogger(__name__)


async def _show_view(target, view: View) -> None:
    await show(target, view.text, view.markup)


async def _return_to(state: FSMContext) -> str:
    rt = (await state.get_data()).get("return_to") or ""
    return rt if rt in RETURN_TARGETS else ""


def _step_after_date(ctx: Ctx) -> str:
    """A customer who already entered and confirmed everything (e.g. came
    back from the review to change the insurance) returns straight to the
    consolidated review; otherwise, on to "Как заполнить данные?"."""
    draft = ctx.draft()
    return "checkout_review" if vehicle_confirmed(ctx, draft) and policyholder_complete(draft) else "method"


async def _order_guard(target, state: FSMContext, ctx: Ctx) -> bool:
    """True (and the order is shown) when this customer's checkout already
    became an order -- no checkout screen may start a second one."""
    if ctx.draft().get("order_id"):
        await go(target, state, ctx, "order")
        if isinstance(target, CallbackQuery):
            await target.answer()
        return True
    return False


# ----------------------------------------------------------------- handlers


async def on_start(message: Message, command: CommandObject, state: FSMContext, ctx: Ctx):
    token = staff.invite_token(command.args)
    record_start(
        ctx.conn,
        ctx.profile,
        telegram_user_id=message.from_user.id,
        telegram_chat_id=message.chat.id,
        telegram_username=message.from_user.username,
        payload=None if token is not None else command.args,  # an invite is never an acquisition source
    )
    await state.clear()
    notice = await _staff_invite(message, ctx, token) if token is not None else None
    role = staff.role_of(ctx.conn, ctx.profile.bot_key, ctx.user_id)
    if role is not None:
        staff.touch_username(ctx.conn, ctx.profile.bot_key, ctx.user_id, message.from_user.username)
    draft = ctx.draft()
    unfinished = unfinished_order(ctx)
    finished = None
    if unfinished is None and draft.get("order_id"):
        if role is None:
            # A paid/finished order: its status screen (policy resend, "➕ ещё одну").
            await go(message, state, ctx, "order")
            return
        finished = order_views.customer_order(ctx.conn, ctx.profile, ctx.user_id, draft["order_id"])
    # Otherwise never a forced resume: an unfinished order or checkout is
    # OFFERED ("↩️ ..."), next to "🚗 Новая страховка".
    view = intro_view(ctx, draft, unfinished, finished_order=finished)
    await message.answer(f"{notice}\n\n{view.text}" if notice else view.text, reply_markup=view.markup)


async def _staff_invite(message: Message, ctx: Ctx, token: str) -> str:
    """/start mgr_<token>: the account that opened the link becomes a
    MANAGER (bound to its real Telegram user id), once."""
    result = staff.consume_invite(
        ctx.conn, ctx.profile.bot_key, token, telegram_user_id=ctx.user_id, username=message.from_user.username
    )
    if result.status == "added":
        if result.created_by and result.created_by != ctx.user_id:
            member = staff.get_member(ctx.conn, ctx.profile.bot_key, ctx.user_id)
            try:
                await message.bot.send_message(result.created_by, texts.STAFF_NEW_MANAGER_NOTICE.format(label=member.label))
            except TelegramAPIError:
                logger.info("Could not notify the inviting owner")
        return texts.STAFF_INVITE_ADDED
    if result.status == "already_staff":
        return texts.STAFF_INVITE_ALREADY
    logger.info("Rejected a manager invite link (invalid, used or expired)")
    return texts.STAFF_INVITE_INVALID


def unfinished_order(ctx: Ctx):
    """This customer's newest order of this bot that still awaits payment."""
    for order in list_telegram_orders(ctx.conn, bot_key=ctx.profile.bot_key, telegram_user_id=ctx.user_id):
        if order.status in UNFINISHED_STATUSES and not order.is_operator_order:
            return order
    return None


def _message_id(callback: CallbackQuery) -> int | None:
    return callback.message.message_id if callback.message else None


async def on_new_checkout(callback: CallbackQuery, callback_data: NewCheckoutCb, state: FSMContext, ctx: Ctx):
    """"🚗 Новая страховка": abandon the current checkout, start fresh.
    Idempotent: the button names the generation it was shown for, so a
    double tap or a replay after the reset never resets a second time."""
    draft = ctx.draft()
    if callback_data.g != checkout_gen(draft):
        await callback.answer()
        await go(callback, state, ctx, "resume")
        return
    reset_checkout(ctx, floor_message_id=_message_id(callback))
    await state.clear()
    await go(callback, state, ctx, "categories")
    await callback.answer()


async def on_menu(callback: CallbackQuery, state: FSMContext, ctx: Ctx):
    if await _order_guard(callback, state, ctx):
        return
    await state.clear()
    await _show_view(callback, categories_view(ctx))
    await callback.answer()


async def on_category(callback: CallbackQuery, callback_data: CategoryCb, state: FSMContext, ctx: Ctx):
    if await _order_guard(callback, state, ctx):
        return
    await state.clear()
    await _show_view(callback, periods_view(ctx, callback_data.code))
    await callback.answer()


async def on_period(callback: CallbackQuery, callback_data: PeriodCb, state: FSMContext, ctx: Ctx):
    if await _order_guard(callback, state, ctx):
        return
    rt = await _return_to(state)  # e.g. "✏️ Период" from the consolidated review
    await state.clear()
    offered = {code for code, _ in offered_categories(ctx)}
    if callback_data.category not in offered:
        await _show_view(callback, categories_view(ctx, texts.CATEGORY_UNAVAILABLE))
        await callback.answer()
        return
    selection = checkout_service.select_category_period(
        ctx.conn, ctx.settings, session_id=ctx.session_id, category_code=callback_data.category, period_code=callback_data.period
    )
    if not selection.ok:
        await _show_view(callback, periods_view(ctx, callback_data.category))
        await callback.answer(selection.error, show_alert=True)
        return
    draft = ctx.draft()
    if rt and start_date_valid(draft):
        await go(callback, state, ctx, rt)  # period changed; the end date was recomputed for the same start
    elif auto_assigns_start_date(draft_country_code(draft)):
        # TR: coverage begins at issuance -- never ask, just compute it the
        # same way "Сегодня" already does (see _apply_start_date) and move
        # straight on, exactly as if the customer had picked "today".
        error = _apply_start_date(ctx, today_in_georgia())
        if error:
            logger.error("TR auto start-date assignment rejected today's date: %s", error)
            await go(callback, state, ctx, "date", rt=rt)
        else:
            await go(callback, state, ctx, rt or _step_after_date(ctx))
    else:
        await go(callback, state, ctx, "date", rt=rt)
    await callback.answer()


def _apply_start_date(ctx: Ctx, start_date: date) -> str | None:
    """None on success, else the (Russian) rejection reason."""
    result = checkout_service.set_fixed_period_start_date(
        ctx.conn, ctx.settings, session_id=ctx.session_id, start_date=start_date, today=today_in_georgia()
    )
    return result.error


async def on_date_choice(callback: CallbackQuery, callback_data: DateCb, state: FSMContext, ctx: Ctx):
    if await _order_guard(callback, state, ctx):
        return
    draft = ctx.draft()
    rt = await _return_to(state)  # e.g. "✏️ Дата начала" from the consolidated review
    if not selection_complete(ctx, draft):
        await go(callback, state, ctx, "categories")
    elif callback_data.choice == "back" and rt:
        await go(callback, state, ctx, rt)
    elif callback_data.choice == "back":
        await state.clear()
        await _show_view(callback, periods_view(ctx, draft["vehicle_category_code"]))
    elif callback_data.choice == "manual":
        await state.set_state(Flow.waiting_start_date)
        await state.set_data({"return_to": rt or None})
        await show(callback, texts.manual_date_prompt(today_in_georgia() + timedelta(days=1)), None)
    elif callback_data.choice in ("today", "tomorrow"):
        today = today_in_georgia()
        error = _apply_start_date(ctx, today if callback_data.choice == "today" else today + timedelta(days=1))
        if error:
            await state.set_state(None)
            await state.set_data({"return_to": rt or None})
            await show(callback, f"{error}.\n\n{texts.DATE_PROMPT}", date_keyboard())
        else:
            await go(callback, state, ctx, rt or _step_after_date(ctx))
    await callback.answer()


async def on_date_text(message: Message, state: FSMContext, ctx: Ctx):
    if await _order_guard(message, state, ctx):
        return
    if not selection_complete(ctx, ctx.draft()):
        await go(message, state, ctx, "categories")
        return
    example = today_in_georgia() + timedelta(days=1)
    parsed = parse_start_date_input(message.text or "")
    if parsed is None:
        await message.answer(texts.invalid_date(example))
        return
    error = _apply_start_date(ctx, parsed)
    if error:
        await message.answer(texts.date_rejected(error, example))
        return
    await go(message, state, ctx, (await _return_to(state)) or _step_after_date(ctx))


async def on_method(callback: CallbackQuery, callback_data: MethodCb, state: FSMContext, ctx: Ctx):
    if await _order_guard(callback, state, ctx):
        return
    draft = ctx.draft()
    if resume_step(ctx, draft) in ("categories", "date"):
        await go(callback, state, ctx, "resume")
    elif callback_data.choice == "documents":
        ctx.merge({"data_entry_method": "documents"})
        ctx.event("bot_document_upload_started")
        await go(callback, state, ctx, "documents")
    else:
        ctx.merge({"data_entry_method": "manual"})
        ctx.event("bot_manual_entry_started")
        await go(callback, state, ctx, "plate")
    await callback.answer()


async def on_nav(callback: CallbackQuery, callback_data: NavCb, state: FSMContext, ctx: Ctx):
    target, rt = callback_data.to, callback_data.rt
    if rt not in RETURN_TARGETS:
        rt = ""
    if target == "restart_confirmed":
        if ctx.draft().get("order_id"):
            await go(callback, state, ctx, "order")  # never an escape hatch from an existing order
        else:
            # "❌ Отменить оформление" confirmed: a real reset, then the start screen.
            fresh = reset_checkout(ctx, floor_message_id=_message_id(callback))
            await state.clear()
            view = intro_view(ctx, fresh)
            await show(callback, f"{texts.CHECKOUT_CANCELLED}\n\n{view.text}", view.markup)
    elif target in NAV_TARGETS:
        await go(callback, state, ctx, target, rt=rt)
    else:
        await go(callback, state, ctx, "resume")
    await callback.answer()


async def on_other_message(message: Message, state: FSMContext, ctx: Ctx):
    view_step = resume_step(ctx, ctx.draft())
    await go(message, state, ctx, view_step, notice=texts.USE_BUTTONS)


async def on_unknown_callback(callback: CallbackQuery, state: FSMContext, ctx: Ctx):
    await go(callback, state, ctx, "resume")
    await callback.answer()


async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """Never shows the customer a raw exception, never logs the exception
    MESSAGE (which could echo user input) -- only its type and the code
    location, which is enough to debug."""
    exc = event.exception
    # file:line in function only -- no source text, no exception message.
    frames = " <- ".join(
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in reversed(traceback.extract_tb(exc.__traceback__)[-4:])
    )
    logger.error("Unhandled %s while handling update %s at %s", type(exc).__name__, event.update.update_id, frames)
    chat_id = None
    if event.update.message:
        chat_id = event.update.message.chat.id
    elif event.update.callback_query:
        chat_id = event.update.callback_query.from_user.id
    if chat_id is not None:
        try:
            await bot.send_message(chat_id, texts.GENERIC_ERROR)
        except Exception:  # noqa: BLE001 -- best effort, never re-raise from the error handler
            logger.warning("Could not deliver the generic error message for update %s", event.update.update_id)
    return True


class StaleCheckoutMiddleware(BaseMiddleware):
    """Buttons on messages sent BEFORE the customer's last reset belong to
    the abandoned checkout -- refused, never able to write into the fresh
    one. (Telegram message ids only grow within a chat.) Order actions
    ("o:") keep their own ownership checks and stay usable."""

    _EXEMPT_PREFIXES = {"o", "mg", "st", "op"}

    async def __call__(self, handler, event: CallbackQuery, data: dict):
        ctx: Ctx | None = data.get("ctx")
        prefix = (event.data or "").split(":", 1)[0]
        if ctx is not None and prefix not in self._EXEMPT_PREFIXES and event.message is not None:
            floor = ctx.draft().get("checkout_floor_msg_id")
            if floor is not None and event.message.message_id < int(floor):
                try:
                    await event.answer(texts.STALE_BUTTON, show_alert=True)
                except TelegramBadRequest:
                    pass
                return None
        return await handler(event, data)


def build_customer_router() -> Router:
    """A fresh Router per Dispatcher (aiogram routers attach to one parent).
    Order matters: state-specific input handlers before the fallbacks."""
    router = Router(name="customer")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.outer_middleware(StaleCheckoutMiddleware())

    # FIRST and without any state filter: /start works from every state.
    router.message.register(on_start, CommandStart())
    router.callback_query.register(on_menu, MenuCb.filter())
    router.callback_query.register(on_category, CategoryCb.filter())
    router.callback_query.register(on_period, PeriodCb.filter())
    router.callback_query.register(on_date_choice, DateCb.filter())
    router.callback_query.register(on_method, MethodCb.filter())
    router.callback_query.register(on_nav, NavCb.filter())
    router.callback_query.register(on_new_checkout, NewCheckoutCb.filter())
    operator_issue.register_customer_side(router)
    router.message.register(on_date_text, Flow.waiting_start_date, F.text)

    vehicle.register(router)
    documents.register(router)
    policyholder.register(router)
    orders.register(router)  # after documents: a photo in the documents step is a vehicle document

    router.message.register(on_other_message)
    router.callback_query.register(on_unknown_callback)
    return router
