"""The bot's conversation, step by step: which screen exists, what it shows,
where "Назад" goes, what comes next, and which step a customer belongs on
given what their draft holds.

Every screen is rebuilt from the persisted draft (app.sessions), never from
anything remembered in memory -- so a restart, a stale button, or a return
days later always lands somewhere consistent. Conversation navigation (the
awaited input, an edit's return target, a list page/search) goes through
aiogram's FSM, which app.telegram_bot.storage keeps on the same DB row.

Business rules are never decided here: categories, periods, prices, dates,
field validation, catalog matching and completeness all come from
app.checkout / app.pricing / app.catalog.
"""

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import date

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.catalog import repository as catalog_repo
from app.catalog.sync import sync_models_on_demand
from app.checkout import service as checkout_service
from app.checkout.rules import (
    category_period_step_completed,
    draft_country_code,
    duration_range_for,
    requires_date_of_birth,
    requires_model_year,
)
from app.countries import COUNTRIES
from app.dates.rules import today_in_georgia
from app.pricing.provider import get_period
from app.sessions.repository import save_draft
from app.telegram_bot import categories, order_views, staff, texts
from app.telegram_bot.context import Ctx
from app.telegram_bot.keyboards import (
    CitizenshipCb,
    DocsCb,
    DocsRetryCb,
    FinalCb,
    IssueCb,
    KeepCb,
    ManufacturerCb,
    MenuCb,
    MethodCb,
    ModelCb,
    ModelPageCb,
    NavCb,
    NewCheckoutCb,
    OrderCb,
    SuggestCb,
    VehicleConfirmCb,
    categories_keyboard,
    date_keyboard,
    keyboard,
    method_keyboard,
    periods_keyboard,
)

Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None


class Flow(StatesGroup):
    # State names are persisted (insurance_sessions.conversation_state) --
    # never rename one; "Flow:waiting_start_date" predates phase 4.
    waiting_start_date = State()
    plate = State()
    vin = State()
    chassis = State()
    manufacturer = State()
    model = State()
    # Turkey-only (app.checkout.rules.requires_model_year) -- never reached
    # for a country that doesn't require it, see next_vehicle_step.
    model_year = State()
    documents = State()
    full_name = State()
    passport = State()
    citizenship = State()
    # Turkey-only (app.checkout.rules.requires_date_of_birth) -- same gating.
    date_of_birth = State()
    email = State()
    phone = State()


# "model_year" IS a VEHICLE_INPUT_STEPS member (gets the same "✓ Keep"
# affordance as plate/vin/manufacturer/model when EDITED from a review
# screen, see vehicle.py::on_keep) even though the forward walk
# (next_vehicle_step) auto-skips it whenever the draft already holds a
# valid value (e.g. from OCR) -- editing and the forward walk are
# independent paths, same as every other vehicle step.
VEHICLE_INPUT_STEPS = ("plate", "vin", "chassis", "manufacturer", "model", "model_year")
POLICY_STEPS = ("full_name", "passport", "citizenship", "date_of_birth", "email", "phone")
# policyholder input step -> draft key (= insurance_orders column name)
POLICY_STEP_FIELD = {
    "full_name": "full_name",
    "passport": "identification_number",
    "citizenship": "citizenship",
    "date_of_birth": "date_of_birth",
    "email": "contact_email",
    "phone": "contact_phone",
}
FIELD_POLICY_STEP = {field: step for step, field in POLICY_STEP_FIELD.items()}
STEP_STATE = {
    "plate": Flow.plate,
    "vin": Flow.vin,
    "chassis": Flow.chassis,
    "manufacturer": Flow.manufacturer,
    "model": Flow.model,
    "model_year": Flow.model_year,
    "documents": Flow.documents,
    "full_name": Flow.full_name,
    "passport": Flow.passport,
    "citizenship": Flow.citizenship,
    "date_of_birth": Flow.date_of_birth,
    "email": Flow.email,
    "phone": Flow.phone,
}
STATE_STEP = {state.state: step for step, state in STEP_STATE.items()}
# Steps a NavCb button may name (anything else is ignored).
NAV_TARGETS = frozenset(
    {
        "categories", "periods", "date", "method", "vehicle_review", "checkout_review", "documents",
        "policyholder_menu", "final_review", "resume", "restart",
    }
    | set(VEHICLE_INPUT_STEPS)
    | set(POLICY_STEPS)
)
RETURN_TARGETS = frozenset({"", "vehicle_review", "checkout_review", "final_review", "policyholder_menu"})

# The standard document set: both sides of the vehicle registration
# document + the passport photo page. Reaching it starts recognition
# automatically (an album, or photos sent one by one).
REQUIRED_DOCUMENT_PHOTOS = 3

_MODELS_PER_PAGE = 8
_MANUFACTURER_BUTTONS = 10
# Draft keys that survive "Начать заново": who the customer is and where
# they came from -- never their vehicle/policyholder/selection data.
_IDENTITY_KEYS = (
    "country_code",
    "channel",
    "bot_key",
    "telegram_user_id",
    "telegram_chat_id",
    "telegram_username",
    "acquisition_source",
)


@dataclass
class View:
    text: str
    markup: Markup = None
    state: State | None = None


# ------------------------------------------------------------ show / go


async def show(target: Message | CallbackQuery, text: str, markup: Markup) -> None:
    """Callback -> edit the pressed message in place (falls back to a new
    message when it can't be edited, or when the screen needs a reply
    keyboard, which inline-message edits can't carry). Message -> reply."""
    if isinstance(target, CallbackQuery):
        message = target.message
        if isinstance(message, Message) and isinstance(markup, (InlineKeyboardMarkup, type(None))):
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
        await target.bot.send_message(target.from_user.id, text, reply_markup=markup)
        return
    await target.answer(text, reply_markup=markup)


async def go(
    target: Message | CallbackQuery,
    fsm: FSMContext,
    ctx: Ctx,
    step: str,
    *,
    rt: str = "",
    notice: str | None = None,
    page: int = 0,
    query: str | None = None,
) -> str:
    """Show `step` (after gating it against the draft) and set the matching
    conversation state. Returns the step actually shown."""
    draft = ctx.draft()
    step = resolve_step(ctx, draft, step, rt)
    view = await build_view(ctx, draft, step, rt=rt, page=page, query=query)
    await fsm.set_state(view.state)
    await fsm.set_data({"return_to": rt or None, "page": page or None, "query": query})
    text = f"{notice}\n\n{view.text}" if notice else view.text
    await show(target, text, view.markup)
    return step


# ------------------------------------------------------------ predicates


def selection_complete(ctx: Ctx, draft: dict) -> bool:
    """A FIXED-period category+period selection with a price (the only
    product shape this bot sells)."""
    if not category_period_step_completed(ctx.settings, draft) or draft.get("price_customer_minor") is None:
        return False
    return duration_range_for(ctx.settings, draft_country_code(draft), draft["vehicle_category_code"]) is None


def start_date_valid(draft: dict) -> bool:
    # A start date chosen days ago may be in the past by now -- never treat
    # it as valid; the customer is asked again.
    start = draft.get("start_date")
    return bool(start and draft.get("end_date")) and date.fromisoformat(start) >= today_in_georgia()


def has_vehicle_data(draft: dict) -> bool:
    return any(draft.get(k) for k in ("registration_number", "identifier", "manufacturer_id", "model_id"))


def vehicle_confirmed(ctx: Ctx, draft: dict) -> bool:
    return draft.get("vehicle_confirmed") is True and not checkout_service.vehicle_missing_fields(ctx.conn, draft)


def policyholder_complete(draft: dict) -> bool:
    return not checkout_service.policyholder_missing_fields(draft)


def resume_step(ctx: Ctx, draft: dict) -> str:
    """The step the customer belongs on, from what the draft holds."""
    if draft.get("order_id"):
        return "order"
    if not selection_complete(ctx, draft):
        return "categories"
    if not start_date_valid(draft):
        return "date"
    if not vehicle_confirmed(ctx, draft):
        documents_flow = draft.get("data_entry_method") == "documents"
        if has_vehicle_data(draft):
            return "checkout_review" if documents_flow else "vehicle_review"
        if documents_flow:
            return "documents"
        return "method"
    missing = checkout_service.policyholder_missing_fields(draft)
    if missing:
        return FIELD_POLICY_STEP[missing[0]]
    return "checkout_review"


def ready_for_order(ctx: Ctx, draft: dict) -> bool:
    """Everything an order needs is in the draft and still valid (the same
    gate the old final review's "✅ Продолжить" applied)."""
    return (
        selection_complete(ctx, draft)
        and start_date_valid(draft)
        and vehicle_confirmed(ctx, draft)
        and policyholder_complete(draft)
    )


# Screens of the former second confirmation ("Проверьте заявку" + its
# policyholder menu): the consolidated review replaced them. Buttons still
# naming them (older messages) land on the review.
_REVIEW_ALIASES = {"final_review": "checkout_review", "policyholder_menu": "checkout_review", "ready": "checkout_review"}


def fixed_policy_steps(ctx: Ctx) -> set[str]:
    """Policyholder steps this bot never asks (fixed contacts in its profile)."""
    fixed = ctx.profile.fixed_contacts()
    return {step for step, field in POLICY_STEP_FIELD.items() if field in fixed}


def resolve_step(ctx: Ctx, draft: dict, step: str, rt: str = "") -> str:
    """Never shows a step whose prerequisites the draft doesn't meet."""
    if draft.get("order_id"):
        # The checkout already became an order: every screen of it now means
        # "that order" -- a new purchase starts only via "➕ Оформить ещё
        # одну страховку" (app.telegram_bot.orders), never implicitly.
        return "order"
    step = _REVIEW_ALIASES.get(step, step)
    rt = _REVIEW_ALIASES.get(rt, rt)
    if step == "resume":
        return resume_step(ctx, draft)
    if step in ("categories", "restart"):
        return step
    if not selection_complete(ctx, draft):
        return "categories"
    if step in ("date", "periods"):
        return step
    if not start_date_valid(draft):
        return "date"
    if step == "model" and not draft.get("manufacturer_id"):
        return "manufacturer"
    if step in ("method", "documents", "vehicle_review", "checkout_review") or step in VEHICLE_INPUT_STEPS:
        return step
    if step in fixed_policy_steps(ctx):
        # This bot fills the field itself -- the step never exists for its customers.
        return rt if rt == "checkout_review" else resume_step(ctx, draft)
    if step in POLICY_STEPS and rt == "checkout_review":
        return step  # a single-field correction from the consolidated review
    if not vehicle_confirmed(ctx, draft):
        if not has_vehicle_data(draft):
            return "method"
        return "checkout_review" if draft.get("data_entry_method") == "documents" else "vehicle_review"
    if step in POLICY_STEPS:
        return step
    return resume_step(ctx, draft)


def back_step(draft: dict, step: str, rt: str) -> str:
    if rt:
        return rt
    if step == "email":
        # Turkey only: date_of_birth sits between citizenship and email --
        # see POLICY_STEPS. Unchanged ("citizenship") for any country that
        # doesn't require it.
        return "date_of_birth" if requires_date_of_birth(draft_country_code(draft)) else "citizenship"
    return {
        "plate": "method",
        "vin": "plate",
        "chassis": "vin",
        "manufacturer": "chassis" if draft.get("identifier_type") == "chassis" else "vin",
        "model": "manufacturer",
        "model_year": "model",
        "documents": "method",
        "vehicle_review": "documents" if draft.get("data_entry_method") == "documents" else "model",
        "checkout_review": "documents",
        "periods": "date",
        "full_name": "checkout_review" if draft.get("data_entry_method") == "documents" else "vehicle_review",
        "passport": "full_name",
        "citizenship": "passport",
        "date_of_birth": "citizenship",
        "phone": "email",
    }.get(step, "resume")


def next_vehicle_step(step: str, rt: str, draft: dict) -> str:
    if step == "manufacturer":
        return "model"  # a model must always be (re)chosen for a new manufacturer
    if step == "model" and requires_model_year(draft_country_code(draft)) and not draft.get("model_year"):
        # Turkey only, and only when not already known (e.g. from OCR) --
        # the SAME "always wins over rt" precedence as manufacturer->model
        # above, so editing just the model still asks for the year if it's
        # still missing.
        return "model_year"
    if rt:
        return rt
    return {
        "plate": "vin", "vin": "manufacturer", "chassis": "manufacturer", "model": "vehicle_review",
        "model_year": "vehicle_review",
    }[step]


def next_policy_step(step: str, rt: str, draft: dict | None = None) -> str:
    """The next policyholder step still needing an answer -- steps whose
    field is already valid (e.g. prefilled from the passport photo and
    shown on the review) are skipped, never asked again."""
    if rt:
        return rt
    order = list(POLICY_STEPS)
    missing = set(checkout_service.policyholder_missing_fields(draft)) if draft is not None else None
    for later in order[order.index(step) + 1 :]:
        if missing is None or POLICY_STEP_FIELD[later] in missing:
            return later
    return "checkout_review"


def after_vehicle_confirmed_step(draft: dict) -> str:
    missing = checkout_service.policyholder_missing_fields(draft)
    return FIELD_POLICY_STEP[missing[0]] if missing else "checkout_review"


# Draft keys that are NOT checkout progress (who the customer is, where they
# came from, this bot's fixed contacts, and the reset bookkeeping below).
_NON_PROGRESS_KEYS = set(_IDENTITY_KEYS) | {"contact_email", "contact_phone", "checkout_gen", "checkout_floor_msg_id"}


def checkout_gen(draft: dict) -> str:
    return str(draft.get("checkout_gen") or "")


def checkout_in_progress(draft: dict) -> bool:
    """True once the customer has chosen/entered/uploaded anything."""
    return any(value not in (None, [], {}, "") for key, value in draft.items() if key not in _NON_PROGRESS_KEYS)


def reset_checkout(ctx: Ctx, *, floor_message_id: int | None = None, order_id: int | None = None) -> dict:
    """Start a FRESH checkout: the draft keeps only who the customer is,
    their acquisition source and this bot's fixed contacts -- every
    selection, vehicle/policyholder value, pending photo, OCR state and the
    old client_checkout_id are dropped (the next order gets a new one).
    Never touches insurance_orders.

    - checkout_gen: a new generation token -- a late OCR/album result of the
      abandoned checkout checks it and discards itself (app.telegram_bot.
      documents);
    - checkout_floor_msg_id: buttons on messages OLDER than this one belong
      to the abandoned checkout and are refused (StaleCheckoutMiddleware);
    - order_id: point the fresh draft at an existing order (explicit
      "↩️ Вернуться к заказу")."""
    draft = ctx.draft()
    fresh = {k: draft[k] for k in _IDENTITY_KEYS if k in draft}
    fresh.update(ctx.profile.fixed_contacts())
    fresh["checkout_gen"] = uuid.uuid4().hex[:12]
    floor = floor_message_id if floor_message_id is not None else draft.get("checkout_floor_msg_id")
    if floor is not None:
        fresh["checkout_floor_msg_id"] = int(floor)
    if order_id is not None:
        fresh["order_id"] = order_id
    save_draft(ctx.conn, ctx.session_id, fresh)
    return fresh


# ------------------------------------------------------------ labels


def category_label(ctx: Ctx, code: str) -> str:
    return categories.category_label(ctx.conn, ctx.profile, code)


def offered_categories(ctx: Ctx) -> list[tuple[str, str]]:
    codes = categories.offered_category_codes(ctx.conn, ctx.settings, ctx.profile)
    if ctx.profile.category_codes is None:
        # Unchanged sort for every bot that doesn't define its own explicit
        # category list (Georgia today): by the global label dict's key
        # order. A bot WITH its own explicit list controls its own display
        # order directly -- re-sorting it against GE's label order would be
        # meaningless for a product set that dict was never designed for.
        order = list(texts.CATEGORY_LABELS)
        codes.sort(key=lambda code: order.index(code) if code in order else len(order))
    return [(code, category_label(ctx, code)) for code in codes]


def manufacturer_name(ctx: Ctx, draft: dict) -> str | None:
    manufacturer = catalog_repo.get_manufacturer(ctx.conn, draft["manufacturer_id"]) if draft.get("manufacturer_id") else None
    return manufacturer.name if manufacturer else None


def model_name(ctx: Ctx, draft: dict) -> str | None:
    model = catalog_repo.get_model(ctx.conn, draft["model_id"]) if draft.get("model_id") else None
    if model is None or model.manufacturer_id != draft.get("manufacturer_id"):
        return None
    return model.name


def is_other(name: str | None) -> bool:
    return name == catalog_repo.OTHER_NAME


def vehicle_make_display(ctx: Ctx, draft: dict) -> str | None:
    """What the customer sees: the document's own brand text when the
    catalog selection is the "Other" fallback, else the catalog name."""
    catalog = manufacturer_name(ctx, draft)
    if is_other(catalog) and draft.get("vehicle_make_text"):
        return draft["vehicle_make_text"]
    return catalog


def vehicle_model_display(ctx: Ctx, draft: dict) -> str | None:
    catalog = model_name(ctx, draft)
    if is_other(catalog) and draft.get("vehicle_model_text"):
        return draft["vehicle_model_text"]
    return catalog


def catalog_fallback_note(ctx: Ctx, draft: dict) -> str | None:
    """ℹ️ line when (part of) the vehicle is not in the catalog and the
    order will carry tpl.ge's "Other" entry instead."""
    parts = []
    if is_other(manufacturer_name(ctx, draft)) and draft.get("vehicle_make_text"):
        parts.append(draft["vehicle_make_text"])
    if is_other(model_name(ctx, draft)) and draft.get("vehicle_model_text"):
        parts.append(draft["vehicle_model_text"])
    return texts.CATALOG_FALLBACK_NOTE.format(name=" / ".join(parts)) if parts else None


def _fmt(value: date) -> str:
    return texts.format_date(value)


_DATE_INPUT_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")


def parse_start_date_input(text: str) -> date | None:
    """DD.MM.YYYY (single-digit day/month tolerated) -> date, or None. Used
    both for the insurance start date (app.telegram_bot.handlers) and the
    policyholder's date of birth (app.telegram_bot.policyholder) -- the one
    human-friendly date format this bot asks for anywhere."""
    match = _DATE_INPUT_RE.match(text or "")
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _nav_row(draft: dict, step: str, rt: str, *, restart: bool = True) -> list:
    row = [(texts.BTN_BACK_ARROW, NavCb(to=back_step(draft, step, rt)))]
    if restart:
        row.append((texts.BTN_RESTART, NavCb(to="restart")))
    return row


def _with_current(text: str, value) -> str:
    return f"{text}\n\n{texts.CURRENT_VALUE.format(value=value)}" if value else text


# ------------------------------------------------------------ views


def categories_view(ctx: Ctx, notice: str | None = None) -> View:
    text = f"{notice}\n\n{texts.CHOOSE_CATEGORY}" if notice else texts.CHOOSE_CATEGORY
    return View(text, categories_keyboard(offered_categories(ctx)))


def periods_view(ctx: Ctx, category_code: str) -> View:
    if category_code not in dict(offered_categories(ctx)):
        return categories_view(ctx, texts.CATEGORY_UNAVAILABLE)
    periods = checkout_service.list_priced_periods(ctx.settings, ctx.profile.country_code, category_code)
    if not periods:
        return categories_view(ctx, texts.NO_PERIODS)
    lines = [texts.price_line(p.label, p.price_rub) for p in periods]
    return View(texts.choose_period(category_label(ctx, category_code), lines), periods_keyboard(category_code, periods))


def current_price_rub(ctx: Ctx) -> tuple[int | None, bool]:
    """(current effective price in RUB, whether it differed from the draft's
    stored one) -- re-read through the pricing provider every time."""
    before = ctx.draft().get("price_customer_minor")
    after = checkout_service.refresh_draft_price(ctx.conn, ctx.settings, session_id=ctx.session_id)
    return (after // 100 if after is not None else None), (before is not None and after != before)


def method_view(ctx: Ctx, draft: dict) -> View:
    period = get_period(ctx.settings, draft_country_code(draft), draft["vehicle_category_code"], draft["period_code"])
    price_rub, _ = current_price_rub(ctx)
    return View(
        texts.selection_summary(
            category_label=category_label(ctx, draft["vehicle_category_code"]),
            start_date=date.fromisoformat(draft["start_date"]),
            period_label=period.label if period else draft["period_code"],
            price_rub=price_rub if price_rub is not None else draft["price_customer_minor"] // 100,
        ),
        method_keyboard(),
    )


def plate_view(draft: dict, rt: str) -> View:
    current = draft.get("registration_number")
    rows = []
    if current:
        rows.append([(texts.BTN_KEEP.format(value=texts.short(current)), KeepCb(step="plate"))])
    rows.append(_nav_row(draft, "plate", rt))
    return View(_with_current(texts.ASK_PLATE, current), keyboard(rows), Flow.plate)


def vin_view(draft: dict, rt: str) -> View:
    current = draft.get("identifier") if draft.get("identifier_type") == "vin" else None
    rows = []
    if current:
        rows.append([(texts.BTN_KEEP.format(value=texts.short(current)), KeepCb(step="vin"))])
    rows.append([(texts.BTN_NO_VIN, NavCb(to="chassis", rt=rt))])
    rows.append(_nav_row(draft, "vin", rt))
    return View(_with_current(texts.ASK_VIN, current), keyboard(rows), Flow.vin)


def chassis_view(draft: dict, rt: str) -> View:
    current = draft.get("identifier") if draft.get("identifier_type") == "chassis" else None
    rows = []
    if current:
        rows.append([(texts.BTN_KEEP.format(value=texts.short(current)), KeepCb(step="chassis"))])
    rows.append([(texts.BTN_HAVE_VIN, NavCb(to="vin", rt=rt))])
    rows.append(_nav_row(draft, "chassis", rt))
    return View(_with_current(texts.ASK_CHASSIS, current), keyboard(rows), Flow.chassis)


def model_year_view(draft: dict, rt: str) -> View:
    """Turkey only (app.checkout.rules.requires_model_year) -- never reached
    for a country that doesn't require it (see next_vehicle_step/resolve_step)."""
    current = draft.get("model_year")
    rows = []
    if current:
        rows.append([(texts.BTN_KEEP.format(value=texts.short(str(current))), KeepCb(step="model_year"))])
    rows.append(_nav_row(draft, "model_year", rt))
    return View(_with_current(texts.ASK_MODEL_YEAR, current), keyboard(rows), Flow.model_year)


def manufacturer_view(ctx: Ctx, draft: dict, rt: str, query: str | None) -> View:
    """Search over the REAL catalog (~1800 manufacturers): the customer types
    a name, matches come back as buttons. Popular brands are offered only as
    labelled shortcuts, and "🔎 Найти другую марку" + "Other" are always
    there -- a short list is never presented as the whole catalog."""
    other = catalog_repo.get_other_manufacturer(ctx.conn)
    found = []
    if query:
        found = [m for m in catalog_repo.search_manufacturers(ctx.conn, query, limit=_MANUFACTURER_BUTTONS + 1) if not is_other(m.name)]
        found = found[:_MANUFACTURER_BUTTONS]
        text = texts.ASK_MANUFACTURER_RESULTS.format(query=query) if found else texts.MANUFACTURER_NOT_FOUND.format(query=query)
    else:
        found = [m for m in catalog_repo.list_manufacturers(ctx.conn) if m.is_popular and not is_other(m.name)][:_MANUFACTURER_BUTTONS]
        text = texts.ASK_MANUFACTURER if found else texts.ASK_MANUFACTURER_SEARCH
        hint = draft.get("vehicle_make_text") or draft.get("ocr_manufacturer_hint")
        if hint and (not draft.get("manufacturer_id") or is_other(manufacturer_name(ctx, draft))):
            text = f"{texts.OCR_HINT.format(value=hint)}\n\n{text}"
    current = vehicle_make_display(ctx, draft)
    buttons = [(m.name, ManufacturerCb(id=m.id)) for m in found]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    extra = [(texts.BTN_SEARCH_AGAIN if query else texts.BTN_FIND_OTHER_MANUFACTURER, NavCb(to="manufacturer", rt=rt))]
    if other is not None:
        extra.append((texts.BTN_USE_OTHER, ManufacturerCb(id=other.id)))
    rows.append(extra)
    if current:
        rows.append([(texts.BTN_KEEP.format(value=texts.short(current)), KeepCb(step="manufacturer"))])
    rows.append(_nav_row(draft, "manufacturer", rt))
    return View(_with_current(text, current), keyboard(rows), Flow.manufacturer)


async def model_view(ctx: Ctx, draft: dict, rt: str, page: int, query: str | None) -> View:
    manufacturer = catalog_repo.get_manufacturer(ctx.conn, draft["manufacturer_id"])
    if manufacturer is None:
        return manufacturer_view(ctx, draft, rt, None)
    if manufacturer.models_never_synced:
        # The same on-demand catalog sync the web model picker uses (a
        # blocking HTTP call to tpl.ge -- run off the event loop).
        await asyncio.to_thread(sync_models_on_demand, ctx.conn, manufacturer)
    models = catalog_repo.list_models(ctx.conn, manufacturer.id)
    other_rows = [[(texts.BTN_OTHER_MANUFACTURER, NavCb(to="manufacturer", rt=rt))], [(texts.BTN_RESTART, NavCb(to="restart"))]]
    if not models:
        return View(texts.MODELS_UNAVAILABLE, keyboard([[("🔄", NavCb(to="model", rt=rt))], *other_rows]), Flow.model)

    other_model = catalog_repo.get_other_model(ctx.conn, manufacturer.id)
    if other_model is not None:
        # The catalog's own "Other" model is always one tap away, never
        # buried on a later page.
        other_rows.insert(0, [(texts.BTN_USE_OTHER, ModelCb(id=other_model.id))])
    models = [m for m in models if other_model is None or m.id != other_model.id]
    models.sort(key=lambda m: m.name.casefold())
    if query:
        wanted = query.casefold()
        models = [m for m in models if wanted in m.name.casefold()]
        real_matches = models
        text = (
            texts.ASK_MODEL_FILTERED.format(manufacturer=manufacturer.name, query=query)
            if real_matches
            else texts.MODEL_NOT_FOUND.format(manufacturer=manufacturer.name, query=query)
        )
    else:
        text = texts.ASK_MODEL.format(manufacturer=manufacturer.name)
        hint = draft.get("vehicle_model_text") or draft.get("ocr_model_hint")
        if hint and (not draft.get("model_id") or is_other(model_name(ctx, draft))):
            text = f"{texts.OCR_HINT.format(value=hint)}\n\n{text}"

    pages = max(1, -(-len(models) // _MODELS_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = models[page * _MODELS_PER_PAGE : (page + 1) * _MODELS_PER_PAGE]
    buttons = [(m.name, ModelCb(id=m.id)) for m in chunk]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if pages > 1:
        pager = []
        if page > 0:
            pager.append((texts.BTN_PREV, ModelPageCb(page=page - 1)))
        pager.append((f"{page + 1}/{pages}", ModelPageCb(page=page)))
        if page < pages - 1:
            pager.append((texts.BTN_NEXT, ModelPageCb(page=page + 1)))
        rows.append(pager)
    current = vehicle_model_display(ctx, draft)
    if current:
        rows.append([(texts.BTN_KEEP.format(value=texts.short(current)), KeepCb(step="model"))])
    rows.extend(other_rows)
    return View(_with_current(text, current), keyboard(rows), Flow.model)


def vehicle_review_text(ctx: Ctx, draft: dict) -> str:
    empty = texts.NOT_RECOGNIZED if draft.get("data_entry_method") == "documents" else texts.NOT_ENTERED
    identifier_type, identifier = draft.get("identifier_type"), draft.get("identifier")
    if identifier and identifier_type == "vin":
        vin, chassis = identifier, texts.CHASSIS_NOT_NEEDED
    elif identifier and identifier_type == "chassis":
        vin, chassis = texts.VIN_NOT_NEEDED, identifier
    else:
        vin = chassis = empty

    def catalog_line(name: str | None, catalog_name: str | None, hint: str | None) -> str:
        if name and is_other(catalog_name) and name != catalog_name:
            return name  # document text; the "Other" fallback is explained below
        if name:
            return f"{name} {texts.CATALOG_MATCHED}"
        if hint:
            return f"«{hint}» {texts.CATALOG_NOT_MATCHED}"
        return empty

    lines = [
        texts.VEHICLE_REVIEW_TITLE,
        "",
        f"Госномер: {draft.get('registration_number') or empty}",
        f"VIN: {vin}",
        f"Шасси: {chassis}",
        f"Марка: {catalog_line(vehicle_make_display(ctx, draft), manufacturer_name(ctx, draft), draft.get('ocr_manufacturer_hint'))}",
        f"Модель: {catalog_line(vehicle_model_display(ctx, draft), model_name(ctx, draft), draft.get('ocr_model_hint'))}",
    ]
    if requires_model_year(draft_country_code(draft)):
        lines.append(f"Год выпуска: {draft.get('model_year') or empty}")
    note = catalog_fallback_note(ctx, draft)
    if note:
        lines += ["", note]
    missing = checkout_service.vehicle_missing_fields(ctx.conn, draft)
    if missing:
        lines += ["", texts.VEHICLE_INCOMPLETE.format(fields=texts.missing_fields_text(missing))]
    return "\n".join(lines)


def vehicle_review_view(ctx: Ctx, draft: dict, rt: str) -> View:
    edit = "vehicle_review"
    rows = [
        [(texts.BTN_CONFIRM, VehicleConfirmCb())],
        [(texts.BTN_EDIT_PLATE, NavCb(to="plate", rt=edit))],
        [(texts.BTN_EDIT_VIN, NavCb(to="vin", rt=edit)), (texts.BTN_EDIT_CHASSIS, NavCb(to="chassis", rt=edit))],
        [(texts.BTN_EDIT_MANUFACTURER, NavCb(to="manufacturer", rt=edit)), (texts.BTN_EDIT_MODEL, NavCb(to="model", rt=edit))],
    ]
    if requires_model_year(draft_country_code(draft)):
        rows.append([(texts.BTN_EDIT_MODEL_YEAR, NavCb(to="model_year", rt=edit))])
    rows += [
        [(texts.BTN_MORE_PHOTOS, NavCb(to="documents", rt=edit))],
        [(texts.BTN_BACK_ARROW, NavCb(to=back_step(draft, "vehicle_review", rt))), (texts.BTN_RESTART, NavCb(to="restart"))],
    ]
    return View(vehicle_review_text(ctx, draft), keyboard(rows))


def checkout_review_view(ctx: Ctx, draft: dict) -> View:
    """ONE screen with everything known so far (vehicle, insurance,
    policyholder) -- after document recognition, and at the end of manual
    entry. Every ✏️ opens a focused single-field step that returns HERE.
    "✅ Всё верно" is the customer's FINAL confirmation: with everything
    complete it creates the order and shows the payment details right away
    (app.telegram_bot.orders.on_review_confirmed); otherwise it asks only
    for what is still missing and comes back here."""
    dash = "—"
    identifier_type, identifier = draft.get("identifier_type"), draft.get("identifier")
    period = get_period(ctx.settings, draft_country_code(draft), draft["vehicle_category_code"], draft["period_code"])
    price_rub, price_changed = current_price_rub(ctx)
    draft = ctx.draft()  # the price refresh may have updated it
    title = texts.CHECKOUT_REVIEW_TITLE if draft.get("data_entry_method") == "documents" else "Проверьте данные:"
    lines = [
        title,
        "",
        "🚗 Автомобиль",
        f"Госномер: {draft.get('registration_number') or dash}",
        f"VIN: {identifier if identifier and identifier_type == 'vin' else dash}",
        f"Шасси: {identifier if identifier and identifier_type == 'chassis' else dash}",
        f"Марка: {vehicle_make_display(ctx, draft) or dash}",
        f"Модель: {vehicle_model_display(ctx, draft) or dash}",
    ]
    if requires_model_year(draft_country_code(draft)):
        lines.append(f"Год выпуска: {draft.get('model_year') or dash}")
    lines += [
        "",
        "📅 Страховка",
        f"Категория: {category_label(ctx, draft['vehicle_category_code'])}",
        f"Период: {period.label if period else draft['period_code']}",
        f"Дата начала: {_fmt(date.fromisoformat(draft['start_date']))}",
    ]
    if draft.get("end_date"):
        lines.append(f"Окончание: {_fmt(date.fromisoformat(draft['end_date']))}")
    if price_rub is not None:
        lines.append(f"Стоимость: {texts.format_rub(price_rub)} ₽")
    lines += [
        "",
        "👤 Страхователь",
        f"ФИО: {draft.get('full_name') or dash}",
        f"Паспорт: {draft.get('identification_number') or dash}",
        f"Гражданство: {texts.citizenship_label(draft.get('citizenship')) or dash}",
    ]
    if requires_date_of_birth(draft_country_code(draft)):
        dob = draft.get("date_of_birth")
        lines.append(f"Дата рождения: {_fmt(date.fromisoformat(dob)) if dob else dash}")
    lines += _contact_lines(ctx, draft, dash)
    note = catalog_fallback_note(ctx, draft)
    if note:
        lines += ["", note]
    missing = checkout_service.vehicle_missing_fields(ctx.conn, draft)
    if missing:
        lines += ["", texts.CHECKOUT_REVIEW_MISSING.format(fields=texts.missing_fields_text(missing))]
    if price_changed:
        lines = [texts.PRICE_UPDATED, ""] + lines
    ctx.event(
        "bot_checkout_review_shown",
        category=draft["vehicle_category_code"],
        period=draft["period_code"],
        price_rub=price_rub,
        price_changed=price_changed,
    )

    rt = "checkout_review"
    fixed = fixed_policy_steps(ctx)
    contact_edits = [(texts.BTN_EDIT_EMAIL, NavCb(to="email", rt=rt))] if "email" not in fixed else []
    if "phone" not in fixed:
        contact_edits.append((texts.BTN_EDIT_PHONE, NavCb(to="phone", rt=rt)))
    if staff.is_staff(ctx.conn, ctx.profile.bot_key, ctx.user_id):
        # staff: issue directly for the customer whose documents these are,
        # or (deliberately) the ordinary customer payment route
        confirm_rows = [
            [(texts.BTN_OP_ISSUE, IssueCb(a="ask", g=checkout_gen(draft)))],
            [(texts.BTN_OP_PAYMENT_ORDER, FinalCb(action="confirm"))],
        ]
    else:
        confirm_rows = [[(texts.BTN_CONFIRM, FinalCb(action="confirm"))]]
    country_code = draft_country_code(draft)
    vehicle_rows = [[(texts.BTN_R_MANUFACTURER, NavCb(to="manufacturer", rt=rt)), (texts.BTN_R_MODEL, NavCb(to="model", rt=rt))]]
    if requires_model_year(country_code):
        vehicle_rows.append([(texts.BTN_R_MODEL_YEAR, NavCb(to="model_year", rt=rt))])
    policyholder_rows = [[(texts.BTN_R_CITIZENSHIP, NavCb(to="citizenship", rt=rt)), (texts.BTN_R_START, NavCb(to="date", rt=rt))]]
    if requires_date_of_birth(country_code):
        policyholder_rows.append([(texts.BTN_R_DATE_OF_BIRTH, NavCb(to="date_of_birth", rt=rt))])
    rows = [
        *confirm_rows,
        [(texts.BTN_R_PLATE, NavCb(to="plate", rt=rt)), (texts.BTN_R_VIN, NavCb(to="vin", rt=rt))],
        *vehicle_rows,
        [(texts.BTN_R_FULL_NAME, NavCb(to="full_name", rt=rt)), (texts.BTN_R_PASSPORT, NavCb(to="passport", rt=rt))],
        *policyholder_rows,
        [(texts.BTN_R_PERIOD, NavCb(to="periods", rt=rt))],
        contact_edits,
        [(texts.BTN_REUPLOAD, NavCb(to="documents", rt=rt))],
        [(texts.BTN_BACK_ARROW, NavCb(to=back_step(draft, "checkout_review", ""))), (texts.BTN_RESTART, NavCb(to="restart"))],
    ]
    return View("\n".join(lines), keyboard(rows))


def documents_view(draft: dict, rt: str) -> View:
    return View(f"{texts.ASK_DOCUMENTS}\n\n{documents_status(draft)}", documents_keyboard(draft, rt), Flow.documents)


def documents_status(draft: dict) -> str:
    """"Фото получено: N из 3" plus what happens next."""
    pending = len(draft.get("pending_document_files") or [])
    counter = texts.DOCUMENTS_COUNTER.format(count=pending, required=REQUIRED_DOCUMENT_PHOTOS)
    if pending and draft.get("ocr_last_batch_failed"):
        return f"{counter}\n{texts.DOCUMENTS_FAILED_PENDING.format(count=pending)}"
    if 0 < pending < REQUIRED_DOCUMENT_PHOTOS:
        return f"{counter}\n{texts.DOCUMENTS_REMAINING.format(required=REQUIRED_DOCUMENT_PHOTOS)}"
    return counter


def documents_keyboard(draft: dict, rt: str) -> InlineKeyboardMarkup:
    """After a failed batch the photos are still pending, but the next step
    is an EXPLICIT retry of that batch (or other photos / manual entry) --
    never the ordinary "✅ Все документы загружены" that invites an
    accidental immediate re-run of a deterministic failure."""
    pending = len(draft.get("pending_document_files") or [])
    rows = []
    if pending and draft.get("ocr_last_batch_failed"):
        rows.append([(texts.BTN_OCR_RETRY, DocsRetryCb(seq=int(draft.get("ocr_batch_seq") or 0)))])
        rows.append([(texts.BTN_OTHER_PHOTOS, DocsCb(action="discard"))])
    elif pending >= REQUIRED_DOCUMENT_PHOTOS:
        # Only reachable when automatic recognition was interrupted (e.g. a
        # bot restart mid-album): an explicit, clearly named way to finish.
        rows.append([(texts.BTN_PROCESS_PENDING, DocsCb(action="done"))])
    # Fewer than 3: no completion button -- the customer sends the rest.
    rows.append([(texts.BTN_ENTER_MANUALLY, MethodCb(choice="manual"))])
    rows.append([(texts.BTN_BACK_ARROW, NavCb(to=back_step(draft, "documents", rt))), (texts.BTN_RESTART, NavCb(to="restart"))])
    return keyboard(rows)


_POLICY_PROMPTS = {
    "full_name": texts.ASK_FULL_NAME,
    "passport": texts.ASK_PASSPORT,
    "citizenship": texts.ASK_CITIZENSHIP,
    "date_of_birth": texts.ASK_DATE_OF_BIRTH,
    "email": texts.ASK_EMAIL,
    "phone": texts.ASK_PHONE,
}
_POLICY_SUGGESTION_KEYS = {
    "full_name": "ocr_policyholder_full_name",
    "passport": "ocr_identification_number",
    "citizenship": "ocr_citizenship",
    # Policyholder-document field, same hint-then-confirm pattern as the
    # three above -- NEVER auto-applied, see app.checkout.service.
    # ocr_draft_update's own docstring on why date_of_birth follows this
    # (not the vehicle-document "write directly" pattern model_year uses).
    "date_of_birth": "ocr_date_of_birth",
}


def policy_suggestion(draft: dict, step: str) -> str | None:
    """The OCR-read value for this step, only if it passes the same
    validator the typed value would -- never an invalid suggestion."""
    key = _POLICY_SUGGESTION_KEYS.get(step)
    raw = draft.get(key) if key else None
    if not raw:
        return None
    value, error = checkout_service.validate_policyholder_field(POLICY_STEP_FIELD[step], str(raw))
    return None if error or value == draft.get(POLICY_STEP_FIELD[step]) else value


def keep_label(step: str, value) -> str:
    shown = texts.citizenship_label(value) if step == "citizenship" else value
    return texts.BTN_KEEP.format(value=texts.short(shown))


def policy_view(draft: dict, step: str, rt: str) -> View:
    current = draft.get(POLICY_STEP_FIELD[step])
    if step == "date_of_birth" and current:
        current = _fmt(date.fromisoformat(current))  # stored as ISO; shown DD.MM.YYYY like every other date here
    shown = texts.citizenship_label(current) if step == "citizenship" else current
    text = _with_current(_POLICY_PROMPTS[step], shown)
    if step == "phone":
        rows = [[KeyboardButton(text=texts.BTN_SHARE_PHONE, request_contact=True)]]
        if current:
            rows.append([KeyboardButton(text=keep_label(step, current))])
        rows.append([KeyboardButton(text=texts.BTN_BACK_TEXT)])
        return View(text, ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True), Flow.phone)

    rows = []
    suggestion = policy_suggestion(draft, step)
    if suggestion:
        if step == "citizenship":
            shown_suggestion = texts.citizenship_label(suggestion)
        elif step == "date_of_birth":
            shown_suggestion = _fmt(date.fromisoformat(suggestion))
        else:
            shown_suggestion = suggestion
        rows.append([(texts.BTN_SUGGEST.format(value=texts.short(shown_suggestion)), SuggestCb(step=step))])
    if current:
        rows.append([(keep_label(step, current), KeepCb(step=step))])
    if step == "citizenship":
        buttons = [(label, CitizenshipCb(i=COUNTRIES.index(canonical))) for label, canonical in texts.COMMON_CITIZENSHIPS]
        rows.extend(buttons[i : i + 2] for i in range(0, len(buttons), 2))
    rows.append(_nav_row(draft, step, rt))
    return View(text, keyboard(rows), STEP_STATE[step])


def _contact_lines(ctx: Ctx, draft: dict, dash: str) -> list[str]:
    """Email/phone review lines -- omitted for a bot that fills them itself."""
    fixed = fixed_policy_steps(ctx)
    lines = []
    if "email" not in fixed:
        lines.append(f"Email: {draft.get('contact_email') or dash}")
    if "phone" not in fixed:
        lines.append(f"Телефон: {draft.get('contact_phone') or dash}")
    return lines


def intro_view(ctx: Ctx, draft: dict, unfinished_order=None, *, finished_order=None) -> View:
    """/start: never a dead end and never a forced resume.
    - an unfinished order exists  -> 🚗 Новая страховка / ↩️ Вернуться к заказу
    - a checkout is in progress   -> 🚗 Новая страховка / ↩️ Продолжить оформление
    - otherwise                   -> 🚗 Оформить страховку
    Staff (checked in the DB, never from the keyboard) additionally get the
    manager rows (📋 Заказы, ⏳ Ожидают оплаты; the owner also 👥 Менеджеры)."""
    text = texts.intro(ctx.profile)
    gen = checkout_gen(draft)
    if finished_order is not None:
        # staff whose last purchase is done: their order stays one tap away
        rows = [
            [(texts.BTN_NEW_INSURANCE, NewCheckoutCb(g=gen))],
            [(texts.BTN_BACK_TO_ORDER, OrderCb(action="resume", order_id=finished_order.id))],
        ]
    elif unfinished_order is not None:
        text = f"{texts.INTRO_UNFINISHED_ORDER.format(number=unfinished_order.public_number)}\n\n{text}"
        rows = [
            [(texts.BTN_NEW_INSURANCE, NewCheckoutCb(g=gen))],
            [(texts.BTN_BACK_TO_ORDER, OrderCb(action="resume", order_id=unfinished_order.id))],
        ]
    elif checkout_in_progress(draft):
        text = f"{texts.INTRO_IN_PROGRESS}\n\n{text}"
        rows = [[(texts.BTN_NEW_INSURANCE, NewCheckoutCb(g=gen))], [(texts.BTN_CONTINUE_CHECKOUT, NavCb(to="resume"))]]
    else:
        rows = [[(texts.BTN_APPLY, MenuCb(action="apply"))]]
    rows += staff_menu_rows(staff.role_of(ctx.conn, ctx.profile.bot_key, ctx.user_id))
    return View(text, keyboard(rows))


def staff_menu_rows(role: str | None) -> list:
    from app.telegram_bot.staff_panel import staff_menu_rows as rows  # the panel owns its buttons

    return rows(role)


def restart_view() -> View:
    rows = [[(texts.BTN_RESTART_CONFIRM, NavCb(to="restart_confirmed"))], [(texts.BTN_CANCEL, NavCb(to="resume"))]]
    return View(texts.RESTART_CONFIRM, keyboard(rows))


def order_view(ctx: Ctx, draft: dict) -> View:
    order = order_views.customer_order(ctx.conn, ctx.profile, ctx.user_id, draft.get("order_id"))
    if order is None:
        return categories_view(ctx)
    text, details_shown = order_views.order_text(ctx.settings, order)
    if order.status == "awaiting_payment":
        ctx.event("bot_payment_details_shown", order_id=order.id, available=details_shown)
    return View(text, order_views.order_keyboard(ctx.conn, order))


async def build_view(ctx: Ctx, draft: dict, step: str, *, rt: str = "", page: int = 0, query: str | None = None) -> View:
    if step == "order":
        return order_view(ctx, draft)
    if step == "categories":
        return categories_view(ctx)
    if step == "date":
        return View(texts.DATE_PROMPT, date_keyboard())
    if step == "method":
        return method_view(ctx, draft)
    if step == "plate":
        return plate_view(draft, rt)
    if step == "vin":
        return vin_view(draft, rt)
    if step == "chassis":
        return chassis_view(draft, rt)
    if step == "manufacturer":
        return manufacturer_view(ctx, draft, rt, query)
    if step == "model":
        return await model_view(ctx, draft, rt, page, query)
    if step == "model_year":
        return model_year_view(draft, rt)
    if step == "checkout_review":
        return checkout_review_view(ctx, draft)
    if step == "periods":
        return periods_view(ctx, draft["vehicle_category_code"])
    if step == "vehicle_review":
        return vehicle_review_view(ctx, draft, rt)
    if step == "documents":
        return documents_view(draft, rt)
    if step in POLICY_STEPS:
        return policy_view(draft, step, rt)
    if step == "restart":
        return restart_view()
    return categories_view(ctx)
