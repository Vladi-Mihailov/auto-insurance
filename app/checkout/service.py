"""Transport-independent pre-order checkout steps.

Each function here is one step of the pre-order draft wizard: it validates
its input against the canonical rules (app.checkout.rules, app.pricing,
app.catalog), writes the result into the SAME insurance_sessions draft the
web checkout uses, and logs the same analytics event. Callers (the FastAPI
routes in app.web.checkout_routes, the Telegram bot in app.telegram_bot)
only translate their own input/output format around these calls -- they
never re-implement any of this.

Errors are returned as user-facing Russian strings (same messages the web
form has always shown), never raised -- a bad selection is an ordinary
outcome, not an exception.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date

from app.analytics.repository import log_event
from app.catalog import repository as catalog_repo
from app.catalog.models import VehicleCategory
from app.checkout.rules import (
    allowed_category_codes,
    category_period_step_completed,
    draft_country_code,
    duration_range_for,
    fixed_duration_date_rule,
    validate_start_date,
)
from app.countries import match_citizenship_text
from app.dates.rules import today_in_georgia
from app.ocr.models import OcrResult, VehicleDataCandidates
from app.pricing.provider import DurationRange, PeriodOption, available_periods, get_period
from app.sessions.repository import get_draft, merge_draft
from app.settings import Settings
from app.validation import (
    IDENTIFIER_TYPES,
    validate_citizenship,
    validate_date_of_birth,
    validate_email,
    validate_engine_power,
    validate_full_name,
    validate_identification_number,
    validate_identifier,
    validate_model_year,
    validate_optional_phone,
    validate_registration_number,
)


class DraftStepMissing(Exception):
    """A step was attempted before the draft holds what it depends on (e.g.
    a start date before any category/period). Callers are expected to guard
    with the matching rules check first; this only fires on a programming
    error or a stale/tampered client."""


def list_offered_categories(conn: sqlite3.Connection, settings: Settings, country_code: str) -> list[VehicleCategory]:
    """Every active catalog category this country's checkout offers -- see
    app.checkout.rules.allowed_category_codes."""
    return catalog_repo.list_categories(conn, allowed_codes=allowed_category_codes(settings, country_code))


def list_priced_periods(settings: Settings, country_code: str, category_code: str) -> list[PeriodOption]:
    """Periods a customer can actually buy -- a configured-but-unpriced
    period is never offered (same filter the web period picker applies)."""
    return [p for p in available_periods(settings, country_code, category_code) if p.is_priced]


@dataclass(frozen=True)
class CategoryPeriodSelection:
    error: str | None
    category: VehicleCategory | None = None
    period: PeriodOption | None = None
    # Non-None only for an EXACT DATE RANGE product (AM) -- see
    # app.checkout.rules.duration_range_for.
    duration_range: DurationRange | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def select_category_period(
    conn: sqlite3.Connection, settings: Settings, *, session_id: str, category_code: str, period_code: str
) -> CategoryPeriodSelection:
    """Step 1: vehicle category + period (+ its RUB price) into the draft."""
    draft = get_draft(conn, session_id) or {}
    country_code = draft_country_code(draft)
    allowed_codes = allowed_category_codes(settings, country_code)
    category = catalog_repo.get_category_by_code(conn, category_code)
    # A category can exist and be active in the catalog while still not
    # being enabled for THIS country (e.g. "truck" for AM/TR right now) --
    # treated identically to an unknown category, never a separate error
    # message, so a tampered/stale submission can't smuggle in a category
    # this country's checkout doesn't actually offer.
    if category is not None and allowed_codes is not None and category.code not in allowed_codes:
        category = None
    if category is None:
        return CategoryPeriodSelection(error="Выберите категорию транспорта")

    duration_range = duration_range_for(settings, country_code, category.code)
    if duration_range is not None:
        # EXACT DATE RANGE product (AM) -- there is no period_code, and
        # start_date/end_date (hence the price) are chosen on the next step.
        merge_draft(
            conn,
            session_id,
            {"vehicle_category_code": category.code, "period_code": None, "price_customer_minor": None},
        )
        log_event(
            conn,
            session_id=session_id,
            order_id=None,
            event_name="category_period_selected",
            properties={"category": category.code, "period": None},
        )
        return CategoryPeriodSelection(error=None, category=category, duration_range=duration_range)

    period = get_period(settings, country_code, category.code, period_code)
    if period is None:
        return CategoryPeriodSelection(error="Выберите один из доступных периодов", category=category)
    if not period.is_priced:
        return CategoryPeriodSelection(
            error="Цена для этого периода пока не настроена — оформление временно недоступно", category=category
        )

    draft_update = {
        "vehicle_category_code": category.code,
        "period_code": period.code,
        "price_customer_minor": period.price_minor,
    }
    # Dependency invalidation: if a start date was already picked on an
    # earlier pass and the period is now changing, the stored end_date was
    # computed for the OLD period -- recompute it now so nothing downstream
    # ever reads a start/end pair that doesn't match the current period.
    if draft.get("start_date"):
        recomputed_end = fixed_duration_date_rule(country_code).compute_end_date(
            date.fromisoformat(draft["start_date"]), period.code
        )
        draft_update["end_date"] = recomputed_end.isoformat()

    merge_draft(conn, session_id, draft_update)
    log_event(
        conn,
        session_id=session_id,
        order_id=None,
        event_name="category_period_selected",
        properties={"category": category.code, "period": period.code},
    )
    return CategoryPeriodSelection(error=None, category=category, period=period)


@dataclass(frozen=True)
class StartDateResult:
    error: str | None
    start_date: date | None = None
    end_date: date | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def set_fixed_period_start_date(
    conn: sqlite3.Connection, settings: Settings, *, session_id: str, start_date: date, today: date
) -> StartDateResult:
    """Step 2 for a FIXED-period product (GE/TR): validate the start date and
    store it with the end date computed from the draft's period. Raises
    DraftStepMissing if the draft has no completed fixed-period selection --
    callers check app.checkout.rules.category_period_step_completed (and
    that duration_range_for is None) first."""
    draft = get_draft(conn, session_id)
    if not category_period_step_completed(settings, draft) or not draft.get("period_code"):
        raise DraftStepMissing("start date requires a completed fixed-period category/period selection")
    country_code = draft_country_code(draft)
    if duration_range_for(settings, country_code, draft["vehicle_category_code"]) is not None:
        raise DraftStepMissing("start date alone is not valid for an exact-date-range product")

    validated, error = validate_start_date(start_date, today)
    if error:
        return StartDateResult(error=error)

    end_date = fixed_duration_date_rule(country_code).compute_end_date(validated, draft["period_code"])
    merge_draft(conn, session_id, {"start_date": validated.isoformat(), "end_date": end_date.isoformat()})
    return StartDateResult(error=None, start_date=validated, end_date=end_date)


# OCR-autofill guards for the three new fields (Step 5) -- an OCR-read value
# goes through the EXACT SAME server-side validators manual entry uses (see
# app.validation), never a separate/looser check. An implausible OCR read
# (0 hp, model_year 3026, a future date_of_birth) is silently dropped here
# rather than autofilled -- the field just stays empty for the user to type
# by hand, same as any other unrecognized field; OCR never blocks on this.
def ocr_engine_power_or_none(raw: int | None) -> int | None:
    if raw is None:
        return None
    value, error = validate_engine_power(str(raw))
    return None if error else value


def ocr_model_year_or_none(raw: int | None) -> int | None:
    if raw is None:
        return None
    value, error = validate_model_year(str(raw), current_year=today_in_georgia().year)
    return None if error else value


def ocr_date_of_birth_or_none(raw: str | None) -> str | None:
    if raw is None:
        return None
    value, error = validate_date_of_birth(raw, today=today_in_georgia())
    return None if error else value.isoformat()


def ocr_draft_update(ocr_result: OcrResult, candidates: VehicleDataCandidates) -> dict:
    """The draft keys one OCR pass produces -- the exact mapping the web
    /documents-soon step has always written (moved here unchanged so the
    Telegram bot applies the same one).

    Partial recognition is still success -- whatever wasn't found/matched
    stays None, and the vehicle review step lets the user fill in or
    correct the rest. Hint text is transient review context only, never a
    stand-in for a real manufacturer_id/model_id (see
    app.ocr.models.VehicleDataCandidates).

    ocr_* keys are initial-autofill-only suggestions for the policyholder
    step -- never re-applied once the user has typed/saved anything of
    their own. citizenship is matched against app.countries.COUNTRIES HERE,
    once, so every reader of this draft key already holds a value safe to
    preselect verbatim -- never the raw, unvalidated OCR string.
    """
    return {
        "data_entry_method": "documents",
        "registration_number": candidates.registration_number,
        "identifier_type": candidates.identifier_type,
        "identifier": candidates.identifier,
        "manufacturer_id": candidates.manufacturer_id,
        "model_id": candidates.model_id,
        "ocr_manufacturer_hint": candidates.manufacturer_text if not candidates.manufacturer_id else None,
        "ocr_model_hint": candidates.model_text if not candidates.model_id else None,
        "ocr_policyholder_full_name": ocr_result.policyholder_full_name,
        "ocr_identification_number": ocr_result.passport_number,
        "ocr_citizenship": match_citizenship_text(ocr_result.citizenship),
        "ocr_driver_full_name": ocr_result.driver_full_name,
        "ocr_owner_full_name": ocr_result.owner_full_name,
        # engine_power/model_year are vehicle-document fields, written
        # directly into their real draft keys -- same as
        # registration_number/manufacturer_id above -- since /vehicle
        # (the very next screen) reads them straight from the draft,
        # with no separate review-hint indirection needed (there's no
        # fuzzy catalog match involved for a plain number the way there
        # is for manufacturer/model text). date_of_birth is a
        # policyholder-document field and follows the ocr_* hint
        # pattern instead, exactly like ocr_policyholder_full_name
        # above (see get_policyholder's initial-autofill-only read).
        "engine_power": ocr_engine_power_or_none(ocr_result.engine_power),
        "model_year": ocr_model_year_or_none(ocr_result.model_year),
        "ocr_date_of_birth": ocr_date_of_birth_or_none(ocr_result.date_of_birth),
    }


def resolve_catalog_selection(
    conn: sqlite3.Connection, manufacturer_id_raw: str, model_id_raw: str
) -> tuple[int | None, str | None, int | None, str | None, dict[str, str]]:
    """Validates manufacturer_id/model_id against the local catalog — never
    trusts these IDs (or any name) from the browser; "Other" is just a
    regular synced row per manufacturer and goes through the exact same
    checks as any other model, no special-casing. Returns (manufacturer_id,
    manufacturer_name, model_id, model_name, errors) — the names are the
    catalog's current names, used by callers as an order-creation-time/
    edit-time SNAPSHOT (see app.orders.repository.create_order)."""
    errors: dict[str, str] = {}

    try:
        manufacturer_id = int(manufacturer_id_raw)
    except (TypeError, ValueError):
        errors["manufacturer_id"] = "Выберите производителя"
        return None, None, None, None, errors

    manufacturer = catalog_repo.get_manufacturer(conn, manufacturer_id)
    if manufacturer is None:
        errors["manufacturer_id"] = "Неизвестный производитель"
        return None, None, None, None, errors

    try:
        model_id = int(model_id_raw)
    except (TypeError, ValueError):
        errors["model_id"] = "Выберите модель"
        return manufacturer_id, manufacturer.name, None, None, errors

    model = catalog_repo.get_model(conn, model_id)
    if model is None or model.manufacturer_id != manufacturer_id:
        errors["model_id"] = "Выберите модель из списка выбранного производителя"
        return manufacturer_id, manufacturer.name, None, None, errors

    return manufacturer_id, manufacturer.name, model_id, model.name, {}


def refresh_draft_price(conn: sqlite3.Connection, settings: Settings, *, session_id: str) -> int | None:
    """Re-reads the draft's CURRENT effective price from the pricing provider
    (config + admin overrides) and stores it on the draft if it changed, so a
    price edited in /admin/prices applies to checkouts still in progress --
    never to an order that already exists (orders keep their own stored
    price). Returns the draft's price in minor units after the refresh.

    Only ever REPLACES a price with a known current one: an admin override
    can re-price an existing period but never un-price it, so when the
    provider has no current price for this selection the draft is left
    exactly as it was and every caller's existing "price is None -> start
    over" guard keeps applying unchanged.

    Only FIXED-period products are re-priced here; an EXACT DATE RANGE
    product (AM) is priced from its dates on the /date step and returned
    unchanged."""
    draft = get_draft(conn, session_id) or {}
    if not category_period_step_completed(settings, draft) or not draft.get("period_code"):
        return draft.get("price_customer_minor")
    country_code = draft_country_code(draft)
    if duration_range_for(settings, country_code, draft["vehicle_category_code"]) is not None:
        return draft.get("price_customer_minor")
    period = get_period(settings, country_code, draft["vehicle_category_code"], draft["period_code"])
    current = period.price_minor if period is not None and period.is_priced else None
    if current is None:
        return draft.get("price_customer_minor")
    if current != draft.get("price_customer_minor"):
        merge_draft(conn, session_id, {"price_customer_minor": current})
    return current


# ---------------------------------------------------------------------------
# Vehicle / policyholder completeness -- the existing validators
# (app.validation) and catalog checks, applied to a draft, for a transport
# that collects fields one at a time (the Telegram bot).
# ---------------------------------------------------------------------------

# Completion rule (same as app.ocr.models.OcrResult.is_complete_for_checkout,
# plus the catalog check every order creation already applies):
# registration number + catalog-valid manufacturer + model of that
# manufacturer + a VIN OR a chassis number (never both required).
VEHICLE_FIELDS = ("registration_number", "identifier", "manufacturer", "model")


def vehicle_missing_fields(conn: sqlite3.Connection, draft: dict) -> list[str]:
    missing = []
    _, error = validate_registration_number(draft.get("registration_number") or "")
    if error:
        missing.append("registration_number")
    identifier_type = draft.get("identifier_type")
    if identifier_type not in IDENTIFIER_TYPES or validate_identifier(draft.get("identifier") or "", identifier_type)[1]:
        missing.append("identifier")
    _, _, _, _, catalog_errors = resolve_catalog_selection(
        conn, str(draft.get("manufacturer_id") or ""), str(draft.get("model_id") or "")
    )
    if "manufacturer_id" in catalog_errors:
        missing.extend(["manufacturer", "model"])
    elif "model_id" in catalog_errors:
        missing.append("model")
    return missing


# Draft keys use the order's own column names, so the order can later be
# created straight from them (app.orders.repository.create_order).
POLICYHOLDER_FIELDS = ("full_name", "identification_number", "citizenship", "contact_email", "contact_phone")


def validate_policyholder_field(field: str, raw: str) -> tuple[str | None, str | None]:
    """One policyholder field through the existing validator for it. Phone
    is REQUIRED here (tpl.ge issuance needs it -- see
    app.integrations.tpl_ge.service.build_application_payload), even though
    the web form treats it as optional."""
    if field == "full_name":
        return validate_full_name(raw)
    if field == "identification_number":
        return validate_identification_number(raw, field_label="Номер паспорта")
    if field == "citizenship":
        return validate_citizenship(raw)
    if field == "contact_email":
        return validate_email(raw)
    if field == "contact_phone":
        if not (raw or "").strip():
            return None, "Телефон: заполните это поле"
        return validate_optional_phone(raw)
    raise ValueError(f"unknown policyholder field {field!r}")


def policyholder_missing_fields(draft: dict) -> list[str]:
    return [
        field
        for field in POLICYHOLDER_FIELDS
        if draft.get(field) is None or validate_policyholder_field(field, str(draft[field]))[1]
    ]


# ---------------------------------------------------------------------------
# Merging a (second, third...) OCR pass into an existing draft
# ---------------------------------------------------------------------------

_OCR_SIMPLE_VEHICLE_KEYS = ("registration_number", "engine_power", "model_year")
_OCR_HINT_KEYS = (
    "ocr_policyholder_full_name",
    "ocr_identification_number",
    "ocr_citizenship",
    "ocr_driver_full_name",
    "ocr_owner_full_name",
    "ocr_date_of_birth",
)


def merge_ocr_update(draft: dict, update: dict) -> tuple[dict, list[str]]:
    """Applies one OCR pass (ocr_draft_update's output) ON TOP OF an
    existing draft without ever making it worse: a field is only ever FILLED
    when it's empty -- an OCR value never replaces an existing value, and an
    empty/unrecognized OCR value never clears one. So several photos enrich
    one draft, and anything the customer typed or already confirmed stays.
    A different non-empty reading of an already-filled field is returned as
    a conflict (the field name) for the caller to surface, never applied.

    Returns (draft keys to write, conflicting field names)."""
    writes: dict = {"data_entry_method": "documents"}
    conflicts: list[str] = []

    for key in _OCR_SIMPLE_VEHICLE_KEYS:
        new = update.get(key)
        if new is None:
            continue
        if draft.get(key) is None:
            writes[key] = new
        elif draft[key] != new:
            conflicts.append(key)

    if update.get("identifier"):
        if not draft.get("identifier"):
            writes["identifier_type"] = update["identifier_type"]
            writes["identifier"] = update["identifier"]
        elif (draft.get("identifier_type"), draft["identifier"]) != (update["identifier_type"], update["identifier"]):
            conflicts.append("identifier")

    # Manufacturer + model move together: a model only means something
    # within its manufacturer.
    new_manufacturer, new_model = update.get("manufacturer_id"), update.get("model_id")
    if draft.get("manufacturer_id") is None:
        if new_manufacturer is not None:
            writes["manufacturer_id"] = new_manufacturer
            writes["model_id"] = new_model
            writes["ocr_model_hint"] = update.get("ocr_model_hint")
            writes["ocr_manufacturer_hint"] = None
        elif update.get("ocr_manufacturer_hint"):
            writes["ocr_manufacturer_hint"] = update["ocr_manufacturer_hint"]
            writes["ocr_model_hint"] = update.get("ocr_model_hint")
    elif new_manufacturer is not None and new_manufacturer != draft["manufacturer_id"]:
        conflicts.append("manufacturer")
    elif draft.get("model_id") is None:
        if new_model is not None and new_manufacturer == draft["manufacturer_id"]:
            writes["model_id"] = new_model
            writes["ocr_model_hint"] = None
        elif update.get("ocr_model_hint"):
            writes["ocr_model_hint"] = update["ocr_model_hint"]
    elif new_model is not None and new_model != draft["model_id"]:
        conflicts.append("model")

    for key in _OCR_HINT_KEYS:
        if update.get(key) is not None and draft.get(key) is None:
            writes[key] = update[key]

    return writes, conflicts


# ---------------------------------------------------------------------------
# Draft -> order (shared by the web /policyholder step and the Telegram bot)
# ---------------------------------------------------------------------------


class OrderFromDraftError(Exception):
    """reason: "no_price" (the selection can't be priced right now) or
    "catalog" (manufacturer/model no longer valid in the catalog)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def create_order_from_draft(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    session_id: str,
    policyholder: dict,
    channel: str = "web",
    bot_key: str | None = None,
    telegram_user_id: int | None = None,
    telegram_chat_id: int | None = None,
    telegram_username: str | None = None,
    acquisition_source: str | None = None,
    client_checkout_id: str | None = None,
    payment_mode: str | None = None,
    created_by_telegram_user_id: int | None = None,
):
    """Creates the order from the session's pre-order draft plus the
    already-validated policyholder fields (full_name, identification_number,
    citizenship, contact_*, and optionally date_of_birth / driver_* /
    owner_* -- the create_order keyword names). The price is re-read from
    the pricing provider first (refresh_draft_price), and manufacturer/model
    are re-resolved at this moment -- this IS the snapshot moment for their
    names (see app.orders.repository.create_order).

    Raises OrderFromDraftError on a draft that can't be ordered, and
    sqlite3.IntegrityError when client_checkout_id already has an order
    (the caller's idempotency path). Does NOT clear the draft or notify
    anyone -- each transport does that its own way."""
    from app.orders.repository import create_order  # local: orders -> checkout import direction stays one-way

    refresh_draft_price(conn, settings, session_id=session_id)
    draft = get_draft(conn, session_id) or {}
    if draft.get("price_customer_minor") is None:
        raise OrderFromDraftError("no_price")
    manufacturer = catalog_repo.get_manufacturer(conn, draft["manufacturer_id"]) if draft.get("manufacturer_id") else None
    model = catalog_repo.get_model(conn, draft["model_id"]) if manufacturer and draft.get("model_id") else None
    if manufacturer is None or model is None or model.manufacturer_id != manufacturer.id:
        raise OrderFromDraftError("catalog")

    return create_order(
        conn,
        session_id=session_id,
        country_code=draft_country_code(draft),
        vehicle_category_code=draft["vehicle_category_code"],
        period_code=draft["period_code"],
        start_date=date.fromisoformat(draft["start_date"]),
        end_date=date.fromisoformat(draft["end_date"]),
        price_customer_minor=draft["price_customer_minor"],
        data_entry_method=draft["data_entry_method"],
        registration_number=draft["registration_number"],
        identifier_type=draft["identifier_type"],
        identifier=draft["identifier"],
        manufacturer_id=manufacturer.id,
        manufacturer_name=manufacturer.name,
        model_id=model.id,
        model_name=model.name,
        customer_currency="RUB",
        purchase_currency="GEL",
        engine_power=draft.get("engine_power"),
        model_year=draft.get("model_year"),
        channel=channel,
        bot_key=bot_key,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        telegram_username=telegram_username,
        acquisition_source=acquisition_source,
        client_checkout_id=client_checkout_id,
        # Document text for a brand/model that fell back to "Other" (bot);
        # always absent for the web checkout.
        vehicle_make_document=draft.get("vehicle_make_text"),
        vehicle_model_document=draft.get("vehicle_model_text"),
        payment_mode=payment_mode,
        created_by_telegram_user_id=created_by_telegram_user_id,
        **policyholder,
    )


def ocr_policyholder_prefill(draft: dict) -> dict:
    """Policyholder fields to fill from the OCR suggestions of the same
    batch (passport name/number/citizenship): only EMPTY fields, and only
    with a value that passes the same validator typing it would -- the
    customer sees and confirms them on the review screen."""
    sources = {
        "full_name": "ocr_policyholder_full_name",
        "identification_number": "ocr_identification_number",
        "citizenship": "ocr_citizenship",
    }
    writes = {}
    for field, source in sources.items():
        if draft.get(field) or not draft.get(source):
            continue
        value, error = validate_policyholder_field(field, str(draft[source]))
        if not error:
            writes[field] = value
    return writes
