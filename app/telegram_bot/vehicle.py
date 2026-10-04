"""Vehicle data input: registration number, VIN or chassis number,
catalog manufacturer + model, and the vehicle review/confirmation.

Every value goes through the existing validators (app.validation) and the
catalog check the web form uses (checkout_service.resolve_catalog_selection)
-- only real catalog manufacturer/model ids are ever stored, including the
catalog's own per-manufacturer "Other" model. Any change resets the
customer's vehicle confirmation: changed data has to be confirmed again.
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.catalog import repository as catalog_repo
from app.checkout import service as checkout_service
from app.telegram_bot import texts
from app.telegram_bot.context import Ctx
from app.telegram_bot.keyboards import KeepCb, ManufacturerCb, ModelCb, ModelPageCb, VehicleConfirmCb
from app.telegram_bot.steps import (
    VEHICLE_INPUT_STEPS,
    Flow,
    after_vehicle_confirmed_step,
    go,
    model_name,
    manufacturer_name,
    next_vehicle_step,
)
from app.validation import validate_identifier, validate_model_year, validate_registration_number
from app.dates.rules import today_in_georgia


async def _rt(state: FSMContext) -> str:
    return (await state.get_data()).get("return_to") or ""


def _changed(ctx: Ctx, updates: dict) -> dict:
    return ctx.merge({**updates, "vehicle_confirmed": False})


async def on_plate_text(message: Message, state: FSMContext, ctx: Ctx):
    value, error = validate_registration_number(message.text or "")
    if error:
        await go(message, state, ctx, "plate", rt=await _rt(state), notice=texts.STEP_INVALID.format(error=error))
        return
    draft = _changed(ctx, {"registration_number": value})
    await go(message, state, ctx, next_vehicle_step("plate", await _rt(state), draft), rt=await _rt(state))


async def _identifier_text(message: Message, state: FSMContext, ctx: Ctx, identifier_type: str, step: str):
    value, error = validate_identifier(message.text or "", identifier_type)
    rt = await _rt(state)
    if error:
        await go(message, state, ctx, step, rt=rt, notice=texts.STEP_INVALID.format(error=error))
        return
    # Exactly one identifier is ever stored (same model as the web form's
    # VIN/chassis toggle) -- entering one replaces the other.
    draft = _changed(ctx, {"identifier_type": identifier_type, "identifier": value})
    await go(message, state, ctx, next_vehicle_step(step, rt, draft), rt=rt)


async def on_vin_text(message: Message, state: FSMContext, ctx: Ctx):
    await _identifier_text(message, state, ctx, "vin", "vin")


async def on_chassis_text(message: Message, state: FSMContext, ctx: Ctx):
    await _identifier_text(message, state, ctx, "chassis", "chassis")


async def on_manufacturer_text(message: Message, state: FSMContext, ctx: Ctx):
    query = (message.text or "").strip()[:64]
    await go(message, state, ctx, "manufacturer", rt=await _rt(state), query=query or None)


async def on_model_text(message: Message, state: FSMContext, ctx: Ctx):
    query = (message.text or "").strip()[:64]
    await go(message, state, ctx, "model", rt=await _rt(state), query=query or None)


async def on_manufacturer(callback: CallbackQuery, callback_data: ManufacturerCb, state: FSMContext, ctx: Ctx):
    manufacturer = catalog_repo.get_manufacturer(ctx.conn, callback_data.id)
    nav = await state.get_data()
    rt = nav.get("return_to") or ""
    if manufacturer is None:
        await go(callback, state, ctx, "manufacturer", rt=rt)
    else:
        draft = ctx.draft()
        updates = {"manufacturer_id": manufacturer.id, "ocr_manufacturer_hint": None}
        if manufacturer.name == catalog_repo.OTHER_NAME:
            # "Other": keep what the brand actually is -- the name the
            # customer just searched for, else the document's text.
            updates["vehicle_make_text"] = nav.get("query") or draft.get("vehicle_make_text") or draft.get("ocr_manufacturer_hint")
        else:
            updates["vehicle_make_text"] = None
        if draft.get("manufacturer_id") != manufacturer.id:
            updates["model_id"] = None  # a model only exists within its manufacturer
            updates["vehicle_model_text"] = draft.get("vehicle_model_text") if manufacturer.name == catalog_repo.OTHER_NAME else None
        _changed(ctx, updates)
        await go(callback, state, ctx, "model", rt=rt)
    await callback.answer()


async def on_model(callback: CallbackQuery, callback_data: ModelCb, state: FSMContext, ctx: Ctx):
    draft = ctx.draft()
    nav = await state.get_data()
    rt = nav.get("return_to") or ""
    _, _, model_id, model_name_, errors = checkout_service.resolve_catalog_selection(
        ctx.conn, str(draft.get("manufacturer_id") or ""), str(callback_data.id)
    )
    if errors:
        await go(callback, state, ctx, "model", rt=rt)
    else:
        updates = {"model_id": model_id, "ocr_model_hint": None}
        if model_name_ == catalog_repo.OTHER_NAME:
            updates["vehicle_model_text"] = nav.get("query") or draft.get("vehicle_model_text") or draft.get("ocr_model_hint")
        else:
            updates["vehicle_model_text"] = None
        _changed(ctx, updates)
        await go(callback, state, ctx, next_vehicle_step("model", rt, draft), rt=rt)
    await callback.answer()


async def on_model_page(callback: CallbackQuery, callback_data: ModelPageCb, state: FSMContext, ctx: Ctx):
    nav = await state.get_data()
    await go(callback, state, ctx, "model", rt=nav.get("return_to") or "", page=callback_data.page, query=nav.get("query"))
    await callback.answer()


async def on_keep(callback: CallbackQuery, callback_data: KeepCb, state: FSMContext, ctx: Ctx):
    """Accept the value already in the draft for this step (if it's still
    valid) and move on -- same as retyping it."""
    step, rt, draft = callback_data.step, await _rt(state), ctx.draft()
    valid = {
        "plate": lambda: not validate_registration_number(draft.get("registration_number") or "")[1],
        "vin": lambda: draft.get("identifier_type") == "vin" and not validate_identifier(draft.get("identifier") or "", "vin")[1],
        "chassis": lambda: draft.get("identifier_type") == "chassis"
        and not validate_identifier(draft.get("identifier") or "", "chassis")[1],
        "manufacturer": lambda: manufacturer_name(ctx, draft) is not None,
        "model": lambda: model_name(ctx, draft) is not None,
        "model_year": lambda: not validate_model_year(str(draft.get("model_year") or ""), current_year=today_in_georgia().year)[1],
    }[step]()
    await go(callback, state, ctx, next_vehicle_step(step, rt, draft) if valid else step, rt=rt)
    await callback.answer()


async def on_model_year_text(message: Message, state: FSMContext, ctx: Ctx):
    value, error = validate_model_year(message.text or "", current_year=today_in_georgia().year)
    rt = await _rt(state)
    if error:
        await go(message, state, ctx, "model_year", rt=rt, notice=texts.STEP_INVALID.format(error=error))
        return
    draft = _changed(ctx, {"model_year": value})
    await go(message, state, ctx, next_vehicle_step("model_year", rt, draft), rt=rt)


async def on_confirm(callback: CallbackQuery, state: FSMContext, ctx: Ctx):
    """"✅ Всё верно" only proceeds when the vehicle is complete by the
    existing rule (registration + catalog manufacturer + catalog model +
    VIN or chassis); otherwise it says what's missing and stays put."""
    draft = ctx.draft()
    missing = checkout_service.vehicle_missing_fields(ctx.conn, draft)
    if missing:
        await callback.answer(texts.VEHICLE_INCOMPLETE.format(fields=texts.missing_fields_text(missing)), show_alert=True)
        await go(callback, state, ctx, "vehicle_review")
        return
    draft = ctx.merge({"vehicle_confirmed": True})
    ctx.event(
        "bot_vehicle_data_confirmed",
        data_entry_method=draft.get("data_entry_method"),
        identifier_type=draft.get("identifier_type"),
    )
    await go(callback, state, ctx, after_vehicle_confirmed_step(draft))
    await callback.answer()


def register(router: Router) -> None:
    router.message.register(on_plate_text, Flow.plate, F.text)
    router.message.register(on_vin_text, Flow.vin, F.text)
    router.message.register(on_chassis_text, Flow.chassis, F.text)
    router.message.register(on_manufacturer_text, Flow.manufacturer, F.text)
    router.message.register(on_model_text, Flow.model, F.text)
    router.message.register(on_model_year_text, Flow.model_year, F.text)
    router.callback_query.register(on_manufacturer, ManufacturerCb.filter())
    router.callback_query.register(on_model, ModelCb.filter())
    router.callback_query.register(on_model_page, ModelPageCb.filter())
    router.callback_query.register(on_keep, KeepCb.filter(F.step.in_(VEHICLE_INPUT_STEPS)))
    router.callback_query.register(on_confirm, VehicleConfirmCb.filter())
