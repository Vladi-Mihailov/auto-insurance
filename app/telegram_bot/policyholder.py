"""Policyholder input: full name (Latin), passport number, citizenship,
email, phone -- exactly the policyholder fields tpl.ge issuance needs
(app.integrations.tpl_ge.service.build_application_payload). Owner and
driver are NOT asked: the existing order model defaults both to "same as
policyholder", which is what that payload resolves them from.

Every value goes through checkout_service.validate_policyholder_field (the
existing app.validation validators; phone required). Stored under the
order's own column names in the draft, ready for order creation.
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from app.checkout import service as checkout_service
from app.countries import COUNTRIES, match_citizenship_text
from app.telegram_bot import texts
from app.telegram_bot.context import Ctx
from app.telegram_bot.keyboards import CitizenshipCb, KeepCb, SuggestCb
from app.telegram_bot.steps import (
    POLICY_STEP_FIELD,
    POLICY_STEPS,
    Flow,
    back_step,
    go,
    keep_label,
    next_policy_step,
    parse_start_date_input,
    policy_suggestion,
)


async def _rt(state: FSMContext) -> str:
    return (await state.get_data()).get("return_to") or ""


async def _accept(target, state: FSMContext, ctx: Ctx, step: str, raw: str) -> None:
    rt = await _rt(state)
    field = POLICY_STEP_FIELD[step]
    value, error = checkout_service.validate_policyholder_field(field, raw)
    if error:
        await go(target, state, ctx, step, rt=rt, notice=texts.STEP_INVALID.format(error=error))
        return
    was_complete = not checkout_service.policyholder_missing_fields(ctx.draft())
    draft = ctx.merge({field: value})
    if not was_complete and not checkout_service.policyholder_missing_fields(draft):
        ctx.event("bot_policyholder_completed")
    if step == "phone" and isinstance(target, Message):
        # Take the phone reply keyboard away before the next (inline) screen.
        await target.answer(texts.PHONE_SAVED, reply_markup=ReplyKeyboardRemove())
    await go(target, state, ctx, next_policy_step(step, rt, draft), rt=rt)


def _citizenship_from_text(text: str) -> str | None:
    return texts.russian_citizenship_to_canonical(text) or match_citizenship_text(text)


async def on_full_name_text(message: Message, state: FSMContext, ctx: Ctx):
    await _accept(message, state, ctx, "full_name", message.text or "")


async def on_passport_text(message: Message, state: FSMContext, ctx: Ctx):
    await _accept(message, state, ctx, "passport", message.text or "")


async def on_citizenship_text(message: Message, state: FSMContext, ctx: Ctx):
    canonical = _citizenship_from_text(message.text or "")
    if canonical is None:
        await go(message, state, ctx, "citizenship", rt=await _rt(state), notice=texts.CITIZENSHIP_NOT_FOUND)
        return
    await _accept(message, state, ctx, "citizenship", canonical)


async def on_date_of_birth_text(message: Message, state: FSMContext, ctx: Ctx):
    parsed = parse_start_date_input(message.text or "")
    if parsed is None:
        notice = texts.STEP_INVALID.format(error="Не удалось распознать дату. Введите её в формате ДД.ММ.ГГГГ")
        await go(message, state, ctx, "date_of_birth", rt=await _rt(state), notice=notice)
        return
    await _accept(message, state, ctx, "date_of_birth", parsed.isoformat())


async def on_email_text(message: Message, state: FSMContext, ctx: Ctx):
    await _accept(message, state, ctx, "email", message.text or "")


async def on_phone_text(message: Message, state: FSMContext, ctx: Ctx):
    text = (message.text or "").strip()
    draft = ctx.draft()
    if text == texts.BTN_BACK_TEXT:
        await message.answer(texts.BTN_BACK_TEXT, reply_markup=ReplyKeyboardRemove())
        await go(message, state, ctx, back_step(draft, "phone", await _rt(state)))
        return
    current = draft.get("contact_phone")
    if current and text == keep_label("phone", current):
        text = current
    await _accept(message, state, ctx, "phone", text)


async def on_phone_contact(message: Message, state: FSMContext, ctx: Ctx):
    contact = message.contact
    # Only the customer's OWN number, via Telegram's share button.
    if contact.user_id != message.from_user.id:
        await go(message, state, ctx, "phone", rt=await _rt(state), notice=texts.PHONE_NOT_OWN)
        return
    phone = contact.phone_number.strip()
    if not phone.startswith("+"):
        phone = "+" + phone
    await _accept(message, state, ctx, "phone", phone)


async def on_keep(callback: CallbackQuery, callback_data: KeepCb, state: FSMContext, ctx: Ctx):
    current = ctx.draft().get(POLICY_STEP_FIELD[callback_data.step])
    await _accept(callback, state, ctx, callback_data.step, str(current or ""))
    await callback.answer()


async def on_suggest(callback: CallbackQuery, callback_data: SuggestCb, state: FSMContext, ctx: Ctx):
    suggestion = policy_suggestion(ctx.draft(), callback_data.step) if callback_data.step in POLICY_STEPS else None
    if suggestion is None:
        await go(callback, state, ctx, callback_data.step if callback_data.step in POLICY_STEPS else "resume")
    else:
        await _accept(callback, state, ctx, callback_data.step, suggestion)
    await callback.answer()


async def on_citizenship_button(callback: CallbackQuery, callback_data: CitizenshipCb, state: FSMContext, ctx: Ctx):
    if 0 <= callback_data.i < len(COUNTRIES):
        await _accept(callback, state, ctx, "citizenship", COUNTRIES[callback_data.i])
    else:
        await go(callback, state, ctx, "citizenship", rt=await _rt(state))
    await callback.answer()


def register(router: Router) -> None:
    router.message.register(on_full_name_text, Flow.full_name, F.text)
    router.message.register(on_passport_text, Flow.passport, F.text)
    router.message.register(on_citizenship_text, Flow.citizenship, F.text)
    router.message.register(on_date_of_birth_text, Flow.date_of_birth, F.text)
    router.message.register(on_email_text, Flow.email, F.text)
    router.message.register(on_phone_contact, Flow.phone, F.contact)
    router.message.register(on_phone_text, Flow.phone, F.text)
    router.callback_query.register(on_keep, KeepCb.filter(F.step.in_(POLICY_STEPS)))
    router.callback_query.register(on_suggest, SuggestCb.filter())
    router.callback_query.register(on_citizenship_button, CitizenshipCb.filter())
