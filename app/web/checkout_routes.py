"""The pre-order checkout wizard: category+period -> dates -> upload/manual
choice -> vehicle details -> policyholder (creates the order).

Split out of app/web/routes.py (which keeps landing/resume/legacy period-date/
summary/payment) so that file doesn't grow into a single giant module — this
one owns everything that happens before an order exists, plus the small JSON
catalog endpoints the vehicle-details picker calls.
"""

import sqlite3
import time
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from app.analytics.repository import log_event
from app.catalog import repository as catalog_repo
from app.catalog.sync import sync_models_on_demand
from app.countries import COUNTRIES, match_citizenship_text
from app.dates.rules import DateRule, FixedDurationDateRule, GeorgiaDateRule, UnknownPeriodCode, today_in_georgia
from app.deps import get_db, get_ocr_provider, get_order_or_404, get_session_id, get_settings
from app.ocr.image import MAX_FILES_PER_RECOGNITION, UploadValidationError, validate_and_normalize_upload
from app.ocr.parser import build_candidates
from app.ocr.provider import OcrProvider, OcrProviderError
from app.orders.models import Order
from app.orders.repository import create_order, set_dates, update_coverage, update_policyholder, update_vehicle_fields
from app.pricing.provider import DurationRange, available_periods, get_duration_range, get_period, resolve_duration_price
from app.sessions.repository import clear_draft, get_draft, merge_draft
from app.validation import (
    validate_citizenship,
    validate_contacts_form,
    validate_date_of_birth,
    validate_driver_form,
    validate_engine_power,
    validate_full_name,
    validate_identification_number,
    validate_model_year,
    validate_owner_form,
    validate_vehicle_details_form,
)
from app.web.step_nav import build_draft_steps, build_order_steps
from app.web.templating import render

router = APIRouter()

DATE_RULE = GeorgiaDateRule()

# The only checkout-level notion of "which countries exist" right now (step 1
# of the multi-country rollout -- see the GE/AM/TR gap-analysis report this
# implements). Georgia is FIXED-period (DATE_RULE above); Turkey is also
# fixed-period but with its own period set/prices-not-yet-set (see
# _FIXED_DURATION_DATE_RULES/config.yaml's pricing.TR); Armenia is EXACT
# DATE RANGE (see _duration_range/_parse_duration_range_dates) -- neither
# AM nor TR has a real customer price yet (out of scope for this step, see
# the gap-analysis report's PricingProvider design), so neither can reach
# order creation through the real UI flow today. That's deliberate, not a
# bug -- see post_policyholder's price guard below.
SUPPORTED_COUNTRY_CODES = ("GE", "AM", "TR")
DEFAULT_COUNTRY_CODE = "GE"

# FIXED-period date rule per country -- GE's own untouched GeorgiaDateRule,
# plus Turkey's own period set via the generic FixedDurationDateRule (see
# app.dates.rules). A country absent here (Armenia) is never fixed-period at
# all right now -- callers must check _duration_range first (see
# _fixed_duration_date_rule's own docstring).
_FIXED_DURATION_DATE_RULES: dict[str, DateRule] = {
    "GE": DATE_RULE,
    "TR": FixedDurationDateRule(day_periods={"30d": 30, "45d": 45, "90d": 90, "180d": 180, "365d": 365}),
}


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _draft_country_code(draft: dict | None) -> str:
    """The country chosen at /start (see app.web.routes.start), carried
    through the whole pre-order draft. Falls back to DEFAULT_COUNTRY_CODE
    for a draft that predates this key (an in-flight session from before
    this change, or any direct/bookmarked entry into the wizard that
    skipped /start entirely) -- this deep into the wizard a missing/unknown
    country is never a user-facing error, just "assume Georgia", exactly
    the implicit behaviour every existing GE checkout already relied on."""
    country_code = (draft or {}).get("country_code")
    return country_code if country_code in SUPPORTED_COUNTRY_CODES else DEFAULT_COUNTRY_CODE


def _allowed_category_codes(settings, country_code: str) -> list[str] | None:
    """The single source of truth for "which internal vehicle_category_code
    values does this country's checkout offer" (see
    app.catalog.repository.list_categories's allowed_codes param, the only
    consumer of this return value). None means unrestricted -- a country
    absent from config.yaml's catalog.enabled_category_codes_by_country
    (Georgia today) sees every active category exactly as before this
    change; AM/TR are explicitly narrowed there instead of here, so enabling
    more categories later is a config edit, never a code change."""
    return settings.catalog.enabled_category_codes_by_country.get(country_code)


# Single source of truth for "which country requires which of the new
# AM/TR-only fields" (see the gap-analysis report's field matrix) -- every
# route that shows, validates, or persists these fields goes through these
# three, never a hardcoded country check duplicated per route/template.

# Categories with no engine at all -- engine_power is never shown/required
# for these regardless of country, even where the country would otherwise
# require it (AM). Currently just trailer; a set (not a single hardcoded
# check) so adding another engine-less category later is a one-line change
# here, not a new branch.
_CATEGORIES_WITHOUT_ENGINE = {"trailer"}


def _requires_engine_power(country_code: str, category_code: str) -> bool:
    # TR no longer requires this for ordinary OSAGO purchase (business
    # decision, 2026-09-06): the source site (strahovka-turkiye.com) only
    # asks for engine/motor info for a separate, unrelated "Turkish plates
    # under customs deposit" service, not for buying the policy itself.
    if category_code in _CATEGORIES_WITHOUT_ENGINE:
        return False
    return country_code == "AM"


def _requires_model_year(country_code: str) -> bool:
    return country_code == "TR"


def _requires_date_of_birth(country_code: str) -> bool:
    return country_code == "TR"


# OCR-autofill guards for the three new fields (Step 5) -- an OCR-read value
# goes through the EXACT SAME server-side validators manual entry uses (see
# app.validation), never a separate/looser check. An implausible OCR read
# (0 hp, model_year 3026, a future date_of_birth) is silently dropped here
# rather than autofilled -- the field just stays empty for the user to type
# by hand, same as any other unrecognized field; OCR never blocks on this.
def _ocr_engine_power_or_none(raw: int | None) -> int | None:
    if raw is None:
        return None
    value, error = validate_engine_power(str(raw))
    return None if error else value


def _ocr_model_year_or_none(raw: int | None) -> int | None:
    if raw is None:
        return None
    value, error = validate_model_year(str(raw), current_year=today_in_georgia().year)
    return None if error else value


def _ocr_date_of_birth_or_none(raw: str | None) -> str | None:
    if raw is None:
        return None
    value, error = validate_date_of_birth(raw, today=today_in_georgia())
    return None if error else value.isoformat()


def _duration_range(settings, country_code: str, category_code: str) -> DurationRange | None:
    """None means (country, category) is a FIXED-period product (GE/TR
    today) -- non-None means EXACT DATE RANGE (AM's passenger_car) where the
    customer picks start_date/end_date directly instead of a period code.
    Thin pass-through to app.pricing.provider.get_duration_range, kept as
    its own helper so every caller in this module goes through the exact
    same check rather than reaching into settings.pricing directly."""
    return get_duration_range(settings, country_code, category_code)


def _fixed_duration_date_rule(country_code: str) -> DateRule:
    """Only ever called after confirming _duration_range(...) is None for
    this (country, category) -- Armenia has no entry here at all (it isn't
    a fixed-period country), so an unrecognized/AM country_code falls back
    to Georgia's own rule, same "assume Georgia" default used everywhere
    else in this module. Callers must not rely on that fallback ever firing
    for AM in practice -- it's a safety net, not a real code path."""
    return _FIXED_DURATION_DATE_RULES.get(country_code, DATE_RULE)


def _category_period_step_completed(settings, draft: dict | None) -> bool:
    """True once /category-period's OWN required data is present: always a
    category, plus -- for a FIXED-period (country, category) -- a
    period_code too. An EXACT DATE RANGE product (AM) has nothing further
    to pick on /category-period at all (see post_category_period): its
    period IS the start_date/end_date chosen on the next step, so a bare
    category is already "done" here."""
    if not draft or draft.get("vehicle_category_code") is None:
        return False
    country_code = _draft_country_code(draft)
    if _duration_range(settings, country_code, draft["vehicle_category_code"]) is not None:
        return True
    return draft.get("period_code") is not None


def _parse_duration_range_dates(
    start_raw: str, end_raw: str, *, today: date, duration_range: DurationRange
) -> tuple[date | None, date | None, str | None]:
    """AM-style EXACT DATE RANGE validation: both dates are direct user
    input (see the module-level comment on _FIXED_DURATION_DATE_RULES) --
    there is no period_code to compute end_date FROM, so this is pure
    validation, not date-math. duration_days uses the exact same "end -
    start, in calendar days" definition GeorgiaDateRule's own period codes
    already imply (see that class's docstring: "15d" means start_date + 15
    days exactly, i.e. end_date - start_date == 15) -- so "10 days" here
    means end_date is start_date + 10 days: duration_range.min_days <=
    (end_date - start_date).days <= duration_range.max_days, inclusive on
    both ends. Returns (start_date, end_date, error) -- the first two are
    None whenever error is not None, same shape as
    _parse_and_validate_start_date."""
    parsed_start, error = _parse_and_validate_start_date(start_raw, today)
    if error:
        return None, None, error

    try:
        parsed_end = date.fromisoformat(end_raw)
    except ValueError:
        return None, None, "Некорректная дата окончания"

    duration_days = (parsed_end - parsed_start).days
    if duration_days < duration_range.min_days:
        return None, None, f"Минимальный срок страхования — {duration_range.min_days} дней"
    if duration_days > duration_range.max_days:
        return None, None, f"Максимальный срок страхования — {duration_range.max_days} дней"
    return parsed_start, parsed_end, None


def _require_draft_keys(draft: dict | None, keys: tuple[str, ...]) -> bool:
    if not draft:
        return False
    return all(draft.get(key) is not None for key in keys)


_EMPTY_CONTACTS = {
    "contact_email": "",
    "contact_telegram": "",
    "contact_phone": "",
    "contact_max": "",
    "contact_other": "",
}


def _parse_and_validate_start_date(raw: str, today: date) -> tuple[date | None, str | None]:
    """Server-side source of truth for "start date can't be in the past" --
    the HTML min= attribute (see date_step.html) is a UX nicety only and
    must never be trusted alone, since a direct POST bypasses it entirely."""
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None, "Некорректная дата"
    if parsed < today:
        return None, "Дата начала не может быть раньше сегодняшнего дня"
    return parsed, None


# ---------------------------------------------------------------------------
# Step 1: vehicle category + period + RUB price
# ---------------------------------------------------------------------------


@router.get("/category-period")
def get_category_period(
    request: Request,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id) or {}
    settings = get_settings()
    country_code = _draft_country_code(draft)
    categories = catalog_repo.list_categories(conn, allowed_codes=_allowed_category_codes(settings, country_code))
    selected_category = draft.get("vehicle_category_code") or "passenger_car"
    duration_range = _duration_range(settings, country_code, selected_category)
    if duration_range is not None:
        # EXACT DATE RANGE product (AM) -- no period list on this screen at
        # all, see category_period.html's duration_range branch; the period
        # itself is chosen via start_date/end_date on the next step.
        periods = []
        selected_period = None
    else:
        # Customer-facing display only shows periods that actually have a
        # confirmed price -- an unpriced period (e.g. TR's 45d/90d/180d/365d
        # right now) stays configured in config.yaml as future availability,
        # but is never offered as a selectable tile. Server-side validation
        # (post_category_period below) is unaffected by this filter -- it
        # keeps resolving the period via get_period()/is_priced directly, so
        # a POST for an unpriced period is still rejected exactly as before.
        periods = [p for p in available_periods(settings, country_code, selected_category) if p.is_priced]
        # Default to the first priced period only when nothing has been
        # chosen yet — never overwrite a period the user (or an earlier
        # draft) already picked. Purely a display default: nothing is
        # written to the draft until the form is actually submitted.
        selected_period = draft.get("period_code") or (periods[0].code if periods else None)
    return render(
        request,
        "category_period.html",
        {
            "categories": categories,
            "selected_category": selected_category,
            "periods": periods,
            "selected_period": selected_period,
            "duration_range": duration_range,
            "steps": build_draft_steps(draft, 1),
            "form_action": "/category-period",
            "back_url": "/",
            "submit_label": "Далее",
        },
    )


@router.get("/api/periods")
def get_periods_for_category(
    category_code: str,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Small JSON helper so the category tiles can refresh the period list
    without a full page reload when the user switches category — same
    JSONResponse pattern as the existing date-preview endpoint. Country
    comes from the draft, same as every other step (see
    _draft_country_code) -- this now needs the session/db deps it didn't
    before to read that draft. Customer-facing, so only priced periods are
    returned here -- same filter as the server-rendered category-period
    screen (see get_category_period); this is what the browser actually
    receives when switching category client-side, so it must stay
    consistent with the initial server-rendered tiles."""
    settings = get_settings()
    draft = get_draft(conn, session_id)
    country_code = _draft_country_code(draft)
    periods = [p for p in available_periods(settings, country_code, category_code) if p.is_priced]
    return JSONResponse(
        [{"code": p.code, "label": p.label, "price_rub": p.price_rub, "is_priced": p.is_priced} for p in periods]
    )


@router.post("/category-period")
def post_category_period(
    request: Request,
    category_code: str = Form(...),
    # Form("") not Form(...): an EXACT DATE RANGE submission (AM) legitimately
    # posts an empty period_code (see category_period.html's duration_range
    # branch) -- Form(...) treats an empty-string form field as outright
    # missing (a Starlette/FastAPI form-parsing quirk, confirmed against a
    # minimal repro), which would 422 every real AM submission before this
    # handler's own code ever got a chance to see it.
    period_code: str = Form(""),
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id) or {}
    settings = get_settings()
    country_code = _draft_country_code(draft)
    allowed_codes = _allowed_category_codes(settings, country_code)
    category = catalog_repo.get_category_by_code(conn, category_code)
    # A category can exist and be active in the catalog while still not
    # being enabled for THIS country (e.g. "truck" for AM/TR right now) --
    # treated identically to an unknown category, never a separate error
    # message, so a tampered/stale POST can't smuggle in a category this
    # country's checkout doesn't actually offer.
    if category is not None and allowed_codes is not None and category.code not in allowed_codes:
        category = None

    duration_range = _duration_range(settings, country_code, category.code) if category else None

    error = None
    period = None
    if category is None:
        error = "Выберите категорию транспорта"
    elif duration_range is not None:
        # EXACT DATE RANGE product (AM) -- nothing else to validate on THIS
        # step at all: there is no period_code, and start_date/end_date are
        # chosen on the next step (see post_date_step's duration_range
        # branch). A category alone is a complete /category-period
        # submission for this kind of product.
        pass
    else:
        period = get_period(settings, country_code, category_code, period_code)
        if period is None:
            error = "Выберите один из доступных периодов"
        elif not period.is_priced:
            error = "Цена для этого периода пока не настроена — оформление временно недоступно"

    if error:
        categories = catalog_repo.list_categories(conn, allowed_codes=allowed_codes)
        # Re-derive duration_range from the RAW submitted category_code (not
        # the possibly-None `category` above) so an unknown-category error
        # still redisplays using whatever the category_code the user
        # actually typed would have meant, same as the periods list below
        # already did before this change.
        redisplay_duration_range = _duration_range(settings, country_code, category_code)
        periods = (
            []
            if redisplay_duration_range is not None
            else [p for p in available_periods(settings, country_code, category_code) if p.is_priced]
        )
        return render(
            request,
            "category_period.html",
            {
                "categories": categories,
                "selected_category": category_code,
                "periods": periods,
                "selected_period": None,
                "duration_range": redisplay_duration_range,
                "error": error,
                "steps": build_draft_steps(draft, 1),
                "form_action": "/category-period",
                "back_url": "/",
                "submit_label": "Далее",
            },
            status_code=422,
        )

    if duration_range is not None:
        draft_update = {
            "vehicle_category_code": category.code,
            "period_code": None,
            "price_customer_minor": None,
        }
        merge_draft(conn, session_id, draft_update)
        log_event(
            conn,
            session_id=session_id,
            order_id=None,
            event_name="category_period_selected",
            properties={"category": category.code, "period": None},
        )
        return _redirect("/date")

    draft_update = {
        "vehicle_category_code": category_code,
        "period_code": period.code,
        "price_customer_minor": period.price_minor,
    }
    # Dependency invalidation: if the user already picked a start date on an
    # earlier pass through this wizard and is now changing the period, the
    # end_date stored alongside it was computed for the OLD period and would
    # otherwise silently go stale until the /date step happened to be
    # resubmitted. Recompute it now so nothing downstream ever reads a
    # start/end pair that don't actually match the current period.
    if draft.get("start_date"):
        recomputed_end = _fixed_duration_date_rule(country_code).compute_end_date(
            date.fromisoformat(draft["start_date"]), period.code
        )
        draft_update["end_date"] = recomputed_end.isoformat()

    merge_draft(conn, session_id, draft_update)
    log_event(
        conn,
        session_id=session_id,
        order_id=None,
        event_name="category_period_selected",
        properties={"category": category_code, "period": period.code},
    )
    return _redirect("/date")


# ---------------------------------------------------------------------------
# Step 2: policy start/end date (pre-order)
# ---------------------------------------------------------------------------


@router.get("/date")
def get_date_step(
    request: Request,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    settings = get_settings()
    if not _category_period_step_completed(settings, draft):
        return _redirect("/category-period")

    country_code = _draft_country_code(draft)
    duration_range = _duration_range(settings, country_code, draft["vehicle_category_code"])

    if duration_range is None:
        start_value = draft.get("start_date")
        # A fresh checkout (draft has no start_date yet) defaults the FIELD
        # DISPLAY to today in Georgia -- never UTC/the browser's own
        # timezone, and never written into the draft here; it only becomes
        # the chosen value once the user actually submits the form. Once a
        # start_date exists in the draft (the user already chose one, even
        # if it was today), it's shown as-is on every later visit -- this
        # default never overwrites a real choice on back/forward navigation.
        effective_start = date.fromisoformat(start_value) if start_value else today_in_georgia()
        end_value = _fixed_duration_date_rule(country_code).compute_end_date(effective_start, draft["period_code"])
        return render(
            request,
            "date_step.html",
            {
                "duration_range": None,
                "start_date": effective_start,
                "end_date": end_value,
                "min_date": today_in_georgia().isoformat(),
                "steps": build_draft_steps(draft, 2),
                "form_action": "/date",
                "back_url": "/category-period",
                "submit_label": "Далее",
                "date_preview_url": "/api/date-preview",
            },
        )

    # EXACT DATE RANGE product (AM): both dates are direct user input.
    # end_date has no fixed period to auto-compute from, but the FIRST time
    # this draft is shown (no end_date stored yet), it's pre-filled from
    # duration_range.default_duration_days -- same "never overwrite a real
    # choice" rule as the FIXED-period branch above: once a real end_date
    # exists in the draft, it's shown as-is on every later visit.
    start_value = draft.get("start_date")
    end_value = draft.get("end_date")
    effective_start = date.fromisoformat(start_value) if start_value else today_in_georgia()
    if end_value:
        effective_end = date.fromisoformat(end_value)
    elif duration_range.default_duration_days is not None:
        effective_end = effective_start + timedelta(days=duration_range.default_duration_days)
    else:
        effective_end = None
    duration_days = (effective_end - effective_start).days if effective_end else None
    return render(
        request,
        "date_step.html",
        {
            "duration_range": duration_range,
            "start_date": effective_start,
            "end_date": effective_end,
            "duration_days": duration_days,
            "min_date": today_in_georgia().isoformat(),
            "steps": build_draft_steps(draft, 2),
            "form_action": "/date",
            "back_url": "/category-period",
            "submit_label": "Далее",
        },
    )


@router.get("/api/date-preview")
def get_date_preview_preorder(
    start: str,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    """FIXED-period countries only (see date_step.html, which never sets
    window.INSURANCE_DATE_PREVIEW_URL -- and so never calls this endpoint --
    for an EXACT DATE RANGE draft): a duration-range draft has no
    period_code, so this 400s exactly like it already did for any draft
    that hasn't reached a period yet, never a 500/KeyError."""
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("period_code",)):
        raise HTTPException(status_code=400, detail="Period not selected yet")

    try:
        start_date = date.fromisoformat(start)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")

    country_code = _draft_country_code(draft)
    try:
        end_date = _fixed_duration_date_rule(country_code).compute_end_date(start_date, draft["period_code"])
    except UnknownPeriodCode as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return JSONResponse({"end_date": end_date.isoformat()})


@router.post("/date")
def post_date_step(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(""),
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    settings = get_settings()
    if not _category_period_step_completed(settings, draft):
        return _redirect("/category-period")

    country_code = _draft_country_code(draft)
    duration_range = _duration_range(settings, country_code, draft["vehicle_category_code"])
    today = today_in_georgia()

    if duration_range is None:
        parsed_start, error = _parse_and_validate_start_date(start_date, today)
        if error:
            return render(
                request,
                "date_step.html",
                {
                    "duration_range": None,
                    "start_date": None,
                    "end_date": None,
                    "min_date": today.isoformat(),
                    "error": error,
                    "steps": build_draft_steps(draft, 2),
                    "form_action": "/date",
                    "back_url": "/category-period",
                    "submit_label": "Далее",
                    "date_preview_url": "/api/date-preview",
                },
                status_code=422,
            )

        computed_end_date = _fixed_duration_date_rule(country_code).compute_end_date(parsed_start, draft["period_code"])
        merge_draft(conn, session_id, {"start_date": parsed_start.isoformat(), "end_date": computed_end_date.isoformat()})
        return _redirect("/method")

    parsed_start, parsed_end, error = _parse_duration_range_dates(
        start_date, end_date, today=today, duration_range=duration_range
    )
    if error:
        return render(
            request,
            "date_step.html",
            {
                "duration_range": duration_range,
                "start_date": None,
                "end_date": None,
                "duration_days": None,
                "min_date": today.isoformat(),
                "error": error,
                "steps": build_draft_steps(draft, 2),
                "form_action": "/date",
                "back_url": "/category-period",
                "submit_label": "Далее",
            },
            status_code=422,
        )

    duration_days = (parsed_end - parsed_start).days
    price_customer_minor = resolve_duration_price(settings, country_code, draft["vehicle_category_code"], duration_days)
    merge_draft(
        conn,
        session_id,
        {
            "start_date": parsed_start.isoformat(),
            "end_date": parsed_end.isoformat(),
            "price_customer_minor": price_customer_minor,
        },
    )
    return _redirect("/method")


# ---------------------------------------------------------------------------
# Step 3: data entry method — documents upload (existing stub) vs manual
# ---------------------------------------------------------------------------


@router.get("/method")
def get_method(
    request: Request,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("start_date", "end_date")):
        return _redirect("/date")
    return render(request, "method.html", {"steps": build_draft_steps(draft, 3)})


@router.post("/method")
def post_method(
    choice: str = Form(...),
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("start_date", "end_date")):
        return _redirect("/date")

    if choice == "manual":
        merge_draft(conn, session_id, {"data_entry_method": "manual"})
        log_event(conn, session_id=session_id, order_id=None, event_name="manual_entry_selected")
        return _redirect("/vehicle")

    merge_draft(conn, session_id, {"data_entry_method": "documents"})
    return _redirect("/documents-soon")


def _documents_upload_context(
    draft: dict, *, ocr_provider: OcrProvider | None, error: str | None = None
) -> dict:
    return {
        "steps": build_draft_steps(draft, 4),
        "ocr_available": ocr_provider is not None,
        "error": error,
        "back_url": "/method",
        "max_files": MAX_FILES_PER_RECOGNITION,
    }


@router.get("/documents-soon")
def get_documents_upload(
    request: Request,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
    ocr_provider: OcrProvider | None = Depends(get_ocr_provider),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("start_date", "end_date")):
        return _redirect("/date")
    return render(request, "documents_soon.html", _documents_upload_context(draft, ocr_provider=ocr_provider))


@router.post("/documents-soon")
def post_documents_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
    ocr_provider: OcrProvider | None = Depends(get_ocr_provider),
):
    # A plain (sync) def, not async def, on purpose: every sync route in
    # this file runs its whole dependency chain (get_db's sqlite3.Connection
    # included) in ONE worker thread via FastAPI's threadpool. Making this
    # handler async instead runs it directly on the event loop while
    # get_db/get_session_id's sync generator still resolves in a worker
    # thread, and sqlite3.Connection (check_same_thread=True by default)
    # then raises "objects created in a thread can only be used in that
    # same thread" the first time this handler touches conn. file.file is
    # the underlying sync file object, so no await is needed to read it.
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("start_date", "end_date")):
        return _redirect("/date")

    if ocr_provider is None:
        return render(
            request,
            "documents_soon.html",
            _documents_upload_context(
                draft,
                ocr_provider=None,
                error="Распознавание документов временно недоступно. Введите данные вручную.",
            ),
            status_code=503,
        )

    # An empty <input multiple> that's still submitted arrives as a single
    # UploadFile with an empty filename, not an empty list -- guard both.
    uploaded = [f for f in files if f.filename]
    if not uploaded:
        return render(
            request,
            "documents_soon.html",
            _documents_upload_context(draft, ocr_provider=ocr_provider, error="Загрузите хотя бы одно фото документа."),
            status_code=422,
        )
    if len(uploaded) > MAX_FILES_PER_RECOGNITION:
        return render(
            request,
            "documents_soon.html",
            _documents_upload_context(
                draft,
                ocr_provider=ocr_provider,
                error=f"Слишком много файлов. Максимум {MAX_FILES_PER_RECOGNITION} за один раз.",
            ),
            status_code=422,
        )

    # Validate every photo BEFORE calling the OCR provider on any of them --
    # same all-or-nothing rule the single-file flow already followed (see
    # test_unsupported_file_rejected_before_any_ocr_call/test_oversized_file_
    # rejected_before_any_ocr_call): one bad photo in the batch means none of
    # them are sent anywhere, never a silent partial submit.
    normalized_images: list[bytes] = []
    for index, upload in enumerate(uploaded, start=1):
        raw_bytes = upload.file.read()
        try:
            normalized_images.append(
                validate_and_normalize_upload(raw_bytes, filename=upload.filename, content_type=upload.content_type)
            )
        except UploadValidationError as exc:
            message = str(exc) if len(uploaded) == 1 else f"Файл {index}: {exc}"
            return render(
                request,
                "documents_soon.html",
                _documents_upload_context(draft, ocr_provider=ocr_provider, error=message),
                status_code=422,
            )

    # ONE OCR call carrying every photo in this batch (see
    # app.ocr.provider.OcrProvider.recognize) -- never one call per photo.
    # The model itself works out which photo is which document type and
    # applies the source rules baked into the prompt (vehicle fields only
    # ever come from a vehicle registration document, never from a
    # passport/license/power-of-attorney also present in the same batch);
    # there is no app-level merge/reconciliation step downstream of this.
    started_at = time.monotonic()
    images = [(image, "image/jpeg") for image in normalized_images]
    try:
        ocr_result = ocr_provider.recognize(images)
    except OcrProviderError as exc:
        # Log the provider/duration/safe error classification only -- never
        # str(exc)/exc.classification's underlying message/body, which could
        # echo back document content a provider included in an error
        # payload, and never the image itself. See app.ocr.provider.classify_error.
        log_event(
            conn,
            session_id=session_id,
            order_id=None,
            event_name="ocr_failed",
            properties={
                "provider": "vision",
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "files_count": len(uploaded),
                "error_type": type(exc).__name__,
                **exc.classification,
            },
        )
        return render(
            request,
            "documents_soon.html",
            _documents_upload_context(
                draft,
                ocr_provider=ocr_provider,
                error="Не удалось обработать документ. Попробуйте ещё раз или введите данные вручную.",
            ),
            status_code=502,
        )

    duration_ms = int((time.monotonic() - started_at) * 1000)

    if ocr_result.fields_found_count == 0:
        log_event(
            conn,
            session_id=session_id,
            order_id=None,
            event_name="ocr_failed",
            properties={
                "provider": ocr_result.provider,
                "duration_ms": duration_ms,
                "files_count": len(uploaded),
                "fields_found_count": 0,
            },
        )
        return render(
            request,
            "documents_soon.html",
            _documents_upload_context(
                draft,
                ocr_provider=ocr_provider,
                error="Не удалось распознать данные документа. Попробуйте другое фото или введите данные вручную.",
            ),
            status_code=422,
        )

    candidates = build_candidates(conn, ocr_result)
    log_event(
        conn,
        session_id=session_id,
        order_id=None,
        event_name="ocr_success" if ocr_result.is_complete_for_checkout else "ocr_partial",
        properties={
            "provider": ocr_result.provider,
            "duration_ms": duration_ms,
            "files_count": len(uploaded),
            "fields_found_count": ocr_result.fields_found_count,
        },
    )

    # Partial recognition is still success (section 18) -- whatever wasn't
    # found/matched stays None, and the existing /vehicle form (reached via
    # the redirect below) lets the user fill in or correct the rest. Hint
    # text is transient review context only, never a stand-in for a real
    # manufacturer_id/model_id (see app.ocr.models.VehicleDataCandidates).
    #
    # ocr_* keys below are /policyholder's initial-autofill-only source (see
    # get_policyholder) -- never re-applied once the user has typed/saved
    # anything of their own (see post_policyholder/get_edit_policyholder).
    # citizenship is matched against app.countries.COUNTRIES HERE, once, so
    # every reader of this draft key already holds a value safe to
    # preselect verbatim -- never the raw, unvalidated OCR string.
    merge_draft(
        conn,
        session_id,
        {
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
            "engine_power": _ocr_engine_power_or_none(ocr_result.engine_power),
            "model_year": _ocr_model_year_or_none(ocr_result.model_year),
            "ocr_date_of_birth": _ocr_date_of_birth_or_none(ocr_result.date_of_birth),
        },
    )
    return _redirect("/vehicle")


# ---------------------------------------------------------------------------
# Step 4 (manual path): vehicle details — registration number, VIN/chassis,
# manufacturer, model.
# ---------------------------------------------------------------------------


def _vehicle_form_context(
    *,
    values: dict,
    errors: dict,
    form_action: str,
    manufacturer_name: str | None,
    model_name: str | None,
    steps: list,
    back_url: str,
    country_code: str,
    category_code: str,
) -> dict:
    return {
        "values": values,
        "errors": errors,
        "form_action": form_action,
        "manufacturer_name": manufacturer_name,
        "model_name": model_name,
        "steps": steps,
        "back_url": back_url,
        "country_code": country_code,
        "requires_engine_power": _requires_engine_power(country_code, category_code),
        "requires_model_year": _requires_model_year(country_code),
    }


def _resolve_catalog_selection(
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


@router.get("/vehicle")
def get_vehicle(
    request: Request,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    # Guard on dates (same condition /method itself guards on) rather than
    # data_entry_method == "manual": reaching /vehicle at all — whether via
    # "Заполнить вручную" on /method, via the OCR success redirect from
    # /documents-soon, or via a direct/URL visit that skipped /method
    # entirely — means there's vehicle data to review here, by definition.
    # Only DEFAULT to "manual" when nothing has been set at all; a
    # "documents" value from a successful OCR pass must survive the user
    # simply opening this review screen (this used to force "manual"
    # unconditionally, silently discarding that the data came from OCR).
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("start_date", "end_date")):
        return _redirect("/date")
    if not draft.get("data_entry_method"):
        draft = merge_draft(conn, session_id, {"data_entry_method": "manual"})

    manufacturer_name = None
    model_name = None
    if draft.get("manufacturer_id"):
        manufacturer = catalog_repo.get_manufacturer(conn, draft["manufacturer_id"])
        manufacturer_name = manufacturer.name if manufacturer else None
    if draft.get("model_id"):
        model = catalog_repo.get_model(conn, draft["model_id"])
        model_name = model.name if model else None

    return render(
        request,
        "vehicle_form.html",
        _vehicle_form_context(
            values=draft,
            errors={},
            form_action="/vehicle",
            manufacturer_name=manufacturer_name,
            model_name=model_name,
            steps=build_draft_steps(draft, 4),
            back_url="/method",
            country_code=_draft_country_code(draft),
            category_code=draft["vehicle_category_code"],
        ),
    )


@router.post("/vehicle")
def post_vehicle(
    request: Request,
    registration_number: str = Form(""),
    identifier_type: str = Form(""),
    identifier: str = Form(""),
    manufacturer_id: str = Form(""),
    model_id: str = Form(""),
    engine_power: str = Form(""),
    model_year: str = Form(""),
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("start_date", "end_date")):
        return _redirect("/date")

    country_code = _draft_country_code(draft)
    form = {
        "registration_number": registration_number,
        "identifier_type": identifier_type,
        "identifier": identifier,
    }
    clean, errors = validate_vehicle_details_form(form)

    resolved_manufacturer_id, manufacturer_name, resolved_model_id, model_name, catalog_errors = (
        _resolve_catalog_selection(conn, manufacturer_id, model_id)
    )
    errors.update(catalog_errors)

    # Country-aware, server-side -- never trust the HTML `required` alone.
    # GE never asks for either field, so both stay None regardless of
    # whatever a tampered/stale submission happened to include.
    engine_power_clean = None
    if _requires_engine_power(country_code, draft["vehicle_category_code"]):
        engine_power_clean, engine_power_error = validate_engine_power(engine_power)
        if engine_power_error:
            errors["engine_power"] = engine_power_error

    model_year_clean = None
    if _requires_model_year(country_code):
        model_year_clean, model_year_error = validate_model_year(model_year, current_year=today_in_georgia().year)
        if model_year_error:
            errors["model_year"] = model_year_error

    if errors:
        return render(
            request,
            "vehicle_form.html",
            _vehicle_form_context(
                values={
                    **form,
                    "manufacturer_id": manufacturer_id,
                    "model_id": model_id,
                    "data_entry_method": draft.get("data_entry_method"),
                    "ocr_manufacturer_hint": draft.get("ocr_manufacturer_hint"),
                    "ocr_model_hint": draft.get("ocr_model_hint"),
                    "engine_power": engine_power,
                    "model_year": model_year,
                },
                errors=errors,
                form_action="/vehicle",
                manufacturer_name=manufacturer_name,
                model_name=None,
                steps=build_draft_steps(draft, 4),
                back_url="/method",
                country_code=country_code,
                category_code=draft["vehicle_category_code"],
            ),
            status_code=422,
        )

    merge_draft(
        conn,
        session_id,
        {
            # Preserve how this data actually got here ("documents" from a
            # successful OCR pass, reviewed/corrected here) rather than
            # stamping "manual" just because this is the confirmation POST —
            # only default to "manual" if somehow nothing was set yet.
            "data_entry_method": draft.get("data_entry_method") or "manual",
            "registration_number": clean["registration_number"],
            "identifier_type": clean["identifier_type"],
            "identifier": clean["identifier"],
            "manufacturer_id": resolved_manufacturer_id,
            "model_id": resolved_model_id,
            "engine_power": engine_power_clean,
            "model_year": model_year_clean,
        },
    )
    log_event(conn, session_id=session_id, order_id=None, event_name="vehicle_data_completed")
    return _redirect("/policyholder")


@router.get("/api/manufacturers")
def api_manufacturers(q: str = "", conn: sqlite3.Connection = Depends(get_db)):
    if q.strip():
        manufacturers = catalog_repo.search_manufacturers(conn, q)
    else:
        manufacturers = catalog_repo.list_manufacturers(conn)
    return JSONResponse([{"id": m.id, "name": m.name, "is_popular": m.is_popular} for m in manufacturers])


@router.get("/api/vehicle-models")
def api_vehicle_models(manufacturer_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """Response shape is {"models": [...], "synced": bool} rather than a
    bare list — "synced": false specifically means "tpl.ge couldn't be
    reached just now, try again", distinct from a genuinely empty (but
    synced) model list. See app.catalog.sync module docstring for the
    never-synced vs. genuinely-empty distinction this exists to preserve.
    """
    manufacturer = catalog_repo.get_manufacturer(conn, manufacturer_id)
    if manufacturer is None:
        return JSONResponse({"models": [], "synced": True})

    if manufacturer.models_never_synced:
        synced = sync_models_on_demand(conn, manufacturer)
        if not synced:
            return JSONResponse({"models": [], "synced": False})

    models = catalog_repo.list_models(conn, manufacturer_id)
    return JSONResponse({"models": [{"id": m.id, "name": m.name} for m in models], "synced": True})


# ---------------------------------------------------------------------------
# Step 5: policyholder ("Страховщик") — creates the order.
# ---------------------------------------------------------------------------


def _driver_context(
    *, same_as: bool, full_name: str = "", identifier: str = "", citizenship: str = "", phone: str = "", email: str = ""
) -> dict:
    return {
        "same_as_policyholder": same_as,
        "full_name": full_name or "",
        "identifier": identifier or "",
        "citizenship": citizenship or "",
        "phone": phone or "",
        "email": email or "",
    }


def _owner_context(
    *,
    same_as: bool,
    entity_type: str = "individual",
    full_name: str = "",
    identifier: str = "",
    citizenship: str = "",
    phone: str = "",
    email: str = "",
) -> dict:
    return {
        "same_as_policyholder": same_as,
        "entity_type": entity_type or "individual",
        "full_name": full_name or "",
        "identifier": identifier or "",
        "citizenship": citizenship or "",
        "phone": phone or "",
        "email": email or "",
    }


def _driver_context_from_submission(form: dict) -> dict:
    return _driver_context(
        same_as=form.get("driver_same_as_policyholder", "yes") != "no",
        full_name=form.get("driver_full_name", ""),
        identifier=form.get("driver_identifier", ""),
        citizenship=form.get("driver_citizenship", ""),
        phone=form.get("driver_phone", ""),
        email=form.get("driver_email", ""),
    )


def _owner_context_from_submission(form: dict) -> dict:
    return _owner_context(
        same_as=form.get("owner_same_as_policyholder", "yes") != "no",
        entity_type=form.get("owner_entity_type", "individual"),
        full_name=form.get("owner_full_name", ""),
        identifier=form.get("owner_identifier", ""),
        citizenship=form.get("owner_citizenship", ""),
        phone=form.get("owner_phone", ""),
        email=form.get("owner_email", ""),
    )


def _driver_context_from_order(order: Order) -> dict:
    return _driver_context(
        same_as=order.driver_same_as_policyholder,
        full_name=order.driver_full_name,
        identifier=order.driver_identifier,
        citizenship=order.driver_citizenship,
        phone=order.driver_phone,
        email=order.driver_email,
    )


def _owner_context_from_order(order: Order) -> dict:
    return _owner_context(
        same_as=order.owner_same_as_policyholder,
        entity_type=order.owner_entity_type,
        full_name=order.owner_full_name,
        identifier=order.owner_identifier,
        citizenship=order.owner_citizenship,
        phone=order.owner_phone,
        email=order.owner_email,
    )


def _policyholder_context(
    *,
    errors: dict,
    full_name: str,
    identification_number: str,
    citizenship: str,
    contacts: dict,
    driver: dict,
    owner: dict,
    steps: list,
    form_action: str,
    back_url: str,
    submit_label: str,
    country_code: str,
    date_of_birth: str = "",
) -> dict:
    return {
        "errors": errors,
        "full_name": full_name,
        "identification_number": identification_number,
        "citizenship": citizenship,
        "countries": COUNTRIES,
        "contacts": contacts,
        "driver": driver,
        "owner": owner,
        "steps": steps,
        "form_action": form_action,
        "back_url": back_url,
        "submit_label": submit_label,
        "date_of_birth": date_of_birth,
        "requires_date_of_birth": _requires_date_of_birth(country_code),
    }


@router.get("/policyholder")
def get_policyholder(
    request: Request,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("manufacturer_id", "model_id")):
        return _redirect("/vehicle")
    # Initial autofill ONLY -- draft OCR hints prefill an otherwise-empty
    # form the very first time this page renders; they never re-apply once
    # the user has submitted anything of their own (see post_policyholder,
    # which always re-renders from the SUBMITTED values on error, never
    # back from these draft hints) or once an order exists (see
    # get_edit_policyholder, which never reads the draft at all).
    return render(
        request,
        "policyholder.html",
        _policyholder_context(
            errors={},
            full_name=draft.get("ocr_policyholder_full_name") or "",
            identification_number=draft.get("ocr_identification_number") or "",
            citizenship=draft.get("ocr_citizenship") or "",
            contacts=_EMPTY_CONTACTS,
            driver=_driver_context(same_as=True, full_name=draft.get("ocr_driver_full_name") or ""),
            owner=_owner_context(same_as=True, full_name=draft.get("ocr_owner_full_name") or ""),
            steps=build_draft_steps(draft, 5),
            form_action="/policyholder",
            back_url="/vehicle",
            submit_label="Продолжить",
            country_code=_draft_country_code(draft),
            date_of_birth=draft.get("ocr_date_of_birth") or "",
        ),
    )


@router.post("/policyholder")
def post_policyholder(
    request: Request,
    full_name: str = Form(""),
    identification_number: str = Form(""),
    citizenship: str = Form(""),
    contact_email: str = Form(""),
    contact_telegram: str = Form(""),
    contact_phone: str = Form(""),
    contact_max: str = Form(""),
    contact_other: str = Form(""),
    driver_same_as_policyholder: str = Form("yes"),
    driver_full_name: str = Form(""),
    driver_identifier: str = Form(""),
    driver_citizenship: str = Form(""),
    driver_phone: str = Form(""),
    driver_email: str = Form(""),
    owner_same_as_policyholder: str = Form("yes"),
    owner_entity_type: str = Form("individual"),
    owner_full_name: str = Form(""),
    owner_identifier: str = Form(""),
    owner_citizenship: str = Form(""),
    owner_phone: str = Form(""),
    owner_email: str = Form(""),
    date_of_birth: str = Form(""),
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("manufacturer_id", "model_id")):
        return _redirect("/vehicle")
    if draft.get("price_customer_minor") is None:
        # No pricing mechanism exists yet for this (country, category) --
        # currently only AM's EXACT DATE RANGE product reaches here with a
        # price of None (see post_category_period's duration_range branch;
        # GE/TR's own is_priced gate at /category-period already prevents
        # this for them). Never create an Order with a NULL price -- every
        # summary/payment template assumes a real integer -- and never
        # invent one (explicitly out of scope, see the gap-analysis
        # report's PricingProvider design). Send the customer back to the
        # start of the wizard rather than let them fill in vehicle/
        # policyholder details for a product that can't be priced or paid
        # for yet.
        log_event(conn, session_id=session_id, order_id=None, event_name="order_blocked_no_price")
        return _redirect("/category-period")

    country_code = _draft_country_code(draft)
    submitted_contacts = {
        "contact_email": contact_email,
        "contact_telegram": contact_telegram,
        "contact_phone": contact_phone,
        "contact_max": contact_max,
        "contact_other": contact_other,
    }
    driver_form = {
        "driver_same_as_policyholder": driver_same_as_policyholder,
        "driver_full_name": driver_full_name,
        "driver_identifier": driver_identifier,
        "driver_citizenship": driver_citizenship,
        "driver_phone": driver_phone,
        "driver_email": driver_email,
    }
    owner_form = {
        "owner_same_as_policyholder": owner_same_as_policyholder,
        "owner_entity_type": owner_entity_type,
        "owner_full_name": owner_full_name,
        "owner_identifier": owner_identifier,
        "owner_citizenship": owner_citizenship,
        "owner_phone": owner_phone,
        "owner_email": owner_email,
    }

    clean_name, name_error = validate_full_name(full_name)
    clean_identification_number, id_error = validate_identification_number(
        identification_number, field_label="Идентификационный номер"
    )
    clean_citizenship, citizenship_error = validate_citizenship(citizenship)
    clean_contacts, errors = validate_contacts_form(submitted_contacts)
    driver_clean, driver_errors = validate_driver_form(driver_form)
    owner_clean, owner_errors = validate_owner_form(owner_form)
    errors.update(driver_errors)
    errors.update(owner_errors)
    if name_error:
        errors["full_name"] = name_error
    if id_error:
        errors["identification_number"] = id_error
    if citizenship_error:
        errors["citizenship"] = citizenship_error

    # Country-aware, server-side -- never trust the HTML `required` alone.
    # GE/AM never ask for this, so it stays None regardless of whatever a
    # tampered/stale submission happened to include.
    clean_dob = None
    if _requires_date_of_birth(country_code):
        clean_dob, dob_error = validate_date_of_birth(date_of_birth, today=today_in_georgia())
        if dob_error:
            errors["date_of_birth"] = dob_error

    if errors:
        return render(
            request,
            "policyholder.html",
            _policyholder_context(
                errors=errors,
                full_name=full_name,
                identification_number=identification_number,
                citizenship=citizenship,
                contacts=submitted_contacts,
                driver=_driver_context_from_submission(driver_form),
                owner=_owner_context_from_submission(owner_form),
                steps=build_draft_steps(draft, 5),
                form_action="/policyholder",
                back_url="/vehicle",
                submit_label="Продолжить",
                country_code=country_code,
                date_of_birth=date_of_birth,
            ),
            status_code=422,
        )

    # Re-resolve manufacturer/model right now, at order-creation time — this
    # IS the snapshot moment (see app.orders.repository.create_order). Also
    # a defensive re-check: if either got deactivated in the (normally tiny)
    # window between /vehicle and here, treat the selection as invalid
    # rather than write a snapshot for something that no longer validates.
    manufacturer = catalog_repo.get_manufacturer(conn, draft["manufacturer_id"])
    model = catalog_repo.get_model(conn, draft["model_id"]) if manufacturer else None
    if manufacturer is None or model is None:
        return _redirect("/vehicle")

    order = create_order(
        conn,
        session_id=session_id,
        country_code=country_code,
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
        full_name=clean_name,
        identification_number=clean_identification_number,
        citizenship=clean_citizenship,
        contact_email=clean_contacts["contact_email"],
        contact_telegram=clean_contacts["contact_telegram"],
        contact_phone=clean_contacts["contact_phone"],
        contact_max=clean_contacts["contact_max"],
        contact_other=clean_contacts["contact_other"],
        customer_currency="RUB",
        purchase_currency="GEL",
        engine_power=draft.get("engine_power"),
        model_year=draft.get("model_year"),
        date_of_birth=clean_dob,
        **driver_clean,
        **owner_clean,
    )
    log_event(conn, session_id=session_id, order_id=order.id, event_name="policyholder_provided")
    # The draft has now been fully consumed into a real order — clear it so
    # a resubmitted POST (browser back + resubmit, double form submission)
    # fails the pre-order guard above instead of creating a duplicate order.
    clear_draft(conn, session_id)
    return _redirect(f"/o/{order.resume_token}/summary")


# ---------------------------------------------------------------------------
# Post-order vehicle edit (from the summary screen's "Изменить данные")
# ---------------------------------------------------------------------------


@router.get("/o/{resume_token}/edit-vehicle")
def get_edit_vehicle(
    request: Request,
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    manufacturer_name = None
    model_name = None
    if order.manufacturer_id:
        manufacturer = catalog_repo.get_manufacturer(conn, order.manufacturer_id)
        manufacturer_name = manufacturer.name if manufacturer else None
    if order.model_id:
        model = catalog_repo.get_model(conn, order.model_id)
        model_name = model.name if model else None

    return render(
        request,
        "vehicle_form.html",
        _vehicle_form_context(
            values={
                "registration_number": order.display_registration_number,
                "identifier_type": order.display_identifier_type,
                "identifier": order.display_identifier,
                "manufacturer_id": order.manufacturer_id,
                "model_id": order.model_id,
                "engine_power": order.engine_power,
                "model_year": order.model_year,
            },
            errors={},
            form_action=f"/o/{order.resume_token}/edit-vehicle",
            manufacturer_name=manufacturer_name,
            model_name=model_name,
            steps=build_order_steps(order, 4),
            back_url=f"/o/{order.resume_token}/summary",
            country_code=order.country_code,
            category_code=order.vehicle_category_code,
        ),
    )


@router.post("/o/{resume_token}/edit-vehicle")
def post_edit_vehicle(
    request: Request,
    resume_token: str,
    registration_number: str = Form(""),
    identifier_type: str = Form(""),
    identifier: str = Form(""),
    manufacturer_id: str = Form(""),
    model_id: str = Form(""),
    engine_power: str = Form(""),
    model_year: str = Form(""),
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    form = {
        "registration_number": registration_number,
        "identifier_type": identifier_type,
        "identifier": identifier,
    }
    clean, errors = validate_vehicle_details_form(form)

    resolved_manufacturer_id, manufacturer_name, resolved_model_id, model_name, catalog_errors = (
        _resolve_catalog_selection(conn, manufacturer_id, model_id)
    )
    errors.update(catalog_errors)

    engine_power_clean = None
    if _requires_engine_power(order.country_code, order.vehicle_category_code):
        engine_power_clean, engine_power_error = validate_engine_power(engine_power)
        if engine_power_error:
            errors["engine_power"] = engine_power_error

    model_year_clean = None
    if _requires_model_year(order.country_code):
        model_year_clean, model_year_error = validate_model_year(model_year, current_year=today_in_georgia().year)
        if model_year_error:
            errors["model_year"] = model_year_error

    if errors:
        return render(
            request,
            "vehicle_form.html",
            _vehicle_form_context(
                values={
                    **form,
                    "manufacturer_id": manufacturer_id,
                    "model_id": model_id,
                    "engine_power": engine_power,
                    "model_year": model_year,
                },
                errors=errors,
                form_action=f"/o/{resume_token}/edit-vehicle",
                manufacturer_name=manufacturer_name,
                model_name=None,
                steps=build_order_steps(order, 4),
                back_url=f"/o/{resume_token}/summary",
                country_code=order.country_code,
                category_code=order.vehicle_category_code,
            ),
            status_code=422,
        )

    update_vehicle_fields(
        conn,
        order.id,
        registration_number=clean["registration_number"],
        identifier_type=clean["identifier_type"],
        identifier=clean["identifier"],
        manufacturer_id=resolved_manufacturer_id,
        manufacturer_name=manufacturer_name,
        model_id=resolved_model_id,
        model_name=model_name,
        engine_power=engine_power_clean,
        model_year=model_year_clean,
    )
    return _redirect(f"/o/{resume_token}/summary")


# ---------------------------------------------------------------------------
# Post-order edits: category+period, date, policyholder. Named edit-* (not
# the bare /period, /date already taken by the LEGACY routes in
# app/web/routes.py for pre-existing old orders) to match the edit-vehicle
# convention and avoid colliding with those. Each edits the SAME order by
# resume_token — never creates a new one.
# ---------------------------------------------------------------------------


@router.get("/o/{resume_token}/edit-coverage")
def get_edit_coverage(
    request: Request,
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    # NOT extended to EXACT DATE RANGE products (AM) in this step -- no real
    # AM order can exist yet (see post_policyholder's price guard), so this
    # only ever renders for a FIXED-period order today. It still renders
    # safely (never a crash) if that changes: an AM order's
    # available_periods() is simply empty, same "скоро будет доступна"
    # message the template already shows for any category with no periods
    # configured -- not the ideal message for a duration-range product, but
    # not a bug either. Editing an AM order's category/duration is future
    # work, not this step's.
    settings = get_settings()
    categories = catalog_repo.list_categories(
        conn, allowed_codes=_allowed_category_codes(settings, order.country_code)
    )
    # Customer-facing, same filter as the pre-order screen (see
    # get_category_period) -- an unpriced period must not reappear here
    # just because the customer is editing an existing order.
    periods = [p for p in available_periods(settings, order.country_code, order.vehicle_category_code) if p.is_priced]
    return render(
        request,
        "category_period.html",
        {
            "categories": categories,
            "selected_category": order.vehicle_category_code,
            "periods": periods,
            "selected_period": order.period_code,
            "steps": build_order_steps(order, 1),
            "form_action": f"/o/{order.resume_token}/edit-coverage",
            "back_url": f"/o/{order.resume_token}/summary",
            "submit_label": "Сохранить",
        },
    )


@router.post("/o/{resume_token}/edit-coverage")
def post_edit_coverage(
    request: Request,
    resume_token: str,
    category_code: str = Form(...),
    period_code: str = Form(...),
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    settings = get_settings()
    allowed_codes = _allowed_category_codes(settings, order.country_code)
    category = catalog_repo.get_category_by_code(conn, category_code)
    if category is not None and allowed_codes is not None and category.code not in allowed_codes:
        category = None
    period = get_period(settings, order.country_code, category_code, period_code) if category else None

    error = None
    if category is None:
        error = "Выберите категорию транспорта"
    elif period is None:
        error = "Выберите один из доступных периодов"
    elif not period.is_priced:
        error = "Цена для этого периода пока не настроена — оформление временно недоступно"

    if error:
        categories = catalog_repo.list_categories(conn, allowed_codes=allowed_codes)
        periods = [p for p in available_periods(settings, order.country_code, category_code) if p.is_priced]
        return render(
            request,
            "category_period.html",
            {
                "categories": categories,
                "selected_category": category_code,
                "periods": periods,
                "selected_period": None,
                "error": error,
                "steps": build_order_steps(order, 1),
                "form_action": f"/o/{resume_token}/edit-coverage",
                "back_url": f"/o/{resume_token}/summary",
                "submit_label": "Сохранить",
            },
            status_code=422,
        )

    update_coverage(conn, order.id, vehicle_category_code=category_code, period_code=period.code, price_customer_minor=period.price_minor)
    # Same dependency invalidation as the pre-order wizard: a period change
    # recomputes end_date from the order's EXISTING start_date -- the order
    # itself (id, token) never changes.
    new_end_date = _fixed_duration_date_rule(order.country_code).compute_end_date(order.start_date, period.code)
    set_dates(conn, order.id, start_date=order.start_date, end_date=new_end_date)
    return _redirect(f"/o/{resume_token}/summary")


@router.get("/o/{resume_token}/edit-date")
def get_edit_date(
    request: Request,
    order: Order = Depends(get_order_or_404),
):
    settings = get_settings()
    duration_range = _duration_range(settings, order.country_code, order.vehicle_category_code)
    if duration_range is None:
        return render(
            request,
            "date_step.html",
            {
                "duration_range": None,
                "start_date": order.start_date,
                "end_date": order.end_date,
                "min_date": today_in_georgia().isoformat(),
                "steps": build_order_steps(order, 2),
                "form_action": f"/o/{order.resume_token}/edit-date",
                "back_url": f"/o/{order.resume_token}/summary",
                "submit_label": "Сохранить",
                "date_preview_url": f"/o/{order.resume_token}/date-preview",
            },
        )

    duration_days = (order.end_date - order.start_date).days if order.start_date and order.end_date else None
    return render(
        request,
        "date_step.html",
        {
            "duration_range": duration_range,
            "start_date": order.start_date,
            "end_date": order.end_date,
            "duration_days": duration_days,
            "min_date": today_in_georgia().isoformat(),
            "steps": build_order_steps(order, 2),
            "form_action": f"/o/{order.resume_token}/edit-date",
            "back_url": f"/o/{order.resume_token}/summary",
            "submit_label": "Сохранить",
        },
    )


@router.post("/o/{resume_token}/edit-date")
def post_edit_date(
    request: Request,
    resume_token: str,
    start_date: str = Form(...),
    end_date: str = Form(""),
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    settings = get_settings()
    duration_range = _duration_range(settings, order.country_code, order.vehicle_category_code)
    today = today_in_georgia()

    if duration_range is None:
        parsed_start, error = _parse_and_validate_start_date(start_date, today)
        if error:
            return render(
                request,
                "date_step.html",
                {
                    "duration_range": None,
                    "start_date": None,
                    "end_date": None,
                    "min_date": today.isoformat(),
                    "error": error,
                    "steps": build_order_steps(order, 2),
                    "form_action": f"/o/{resume_token}/edit-date",
                    "back_url": f"/o/{resume_token}/summary",
                    "submit_label": "Сохранить",
                    "date_preview_url": f"/o/{resume_token}/date-preview",
                },
                status_code=422,
            )

        new_end_date = _fixed_duration_date_rule(order.country_code).compute_end_date(parsed_start, order.period_code)
        set_dates(conn, order.id, start_date=parsed_start, end_date=new_end_date)
        return _redirect(f"/o/{resume_token}/summary")

    parsed_start, parsed_end, error = _parse_duration_range_dates(
        start_date, end_date, today=today, duration_range=duration_range
    )
    if error:
        return render(
            request,
            "date_step.html",
            {
                "duration_range": duration_range,
                "start_date": None,
                "end_date": None,
                "duration_days": None,
                "min_date": today.isoformat(),
                "error": error,
                "steps": build_order_steps(order, 2),
                "form_action": f"/o/{resume_token}/edit-date",
                "back_url": f"/o/{resume_token}/summary",
                "submit_label": "Сохранить",
            },
            status_code=422,
        )

    duration_days = (parsed_end - parsed_start).days
    price_customer_minor = resolve_duration_price(settings, order.country_code, order.vehicle_category_code, duration_days)
    set_dates(conn, order.id, start_date=parsed_start, end_date=parsed_end, price_customer_minor=price_customer_minor)
    return _redirect(f"/o/{resume_token}/summary")


@router.get("/o/{resume_token}/edit-policyholder")
def get_edit_policyholder(
    request: Request,
    order: Order = Depends(get_order_or_404),
):
    # Never touches the pre-order draft/OCR hints -- an existing order's
    # OWN saved values always win here (see the OCR task report's priority
    # order: saved Order value > submitted form value > OCR autofill >
    # empty). OCR only ever pre-fills the ONE-TIME /policyholder GET before
    # an order exists (see get_policyholder above).
    return render(
        request,
        "policyholder.html",
        _policyholder_context(
            errors={},
            full_name=order.full_name,
            identification_number=order.identification_number or "",
            citizenship=order.citizenship or "",
            contacts=order.contact_form_values,
            driver=_driver_context_from_order(order),
            owner=_owner_context_from_order(order),
            steps=build_order_steps(order, 5),
            form_action=f"/o/{order.resume_token}/edit-policyholder",
            back_url=f"/o/{order.resume_token}/summary",
            submit_label="Сохранить",
            country_code=order.country_code,
            date_of_birth=order.date_of_birth.isoformat() if order.date_of_birth else "",
        ),
    )


@router.post("/o/{resume_token}/edit-policyholder")
def post_edit_policyholder(
    request: Request,
    resume_token: str,
    full_name: str = Form(""),
    identification_number: str = Form(""),
    citizenship: str = Form(""),
    contact_email: str = Form(""),
    contact_telegram: str = Form(""),
    contact_phone: str = Form(""),
    contact_max: str = Form(""),
    contact_other: str = Form(""),
    driver_same_as_policyholder: str = Form("yes"),
    driver_full_name: str = Form(""),
    driver_identifier: str = Form(""),
    driver_citizenship: str = Form(""),
    driver_phone: str = Form(""),
    driver_email: str = Form(""),
    owner_same_as_policyholder: str = Form("yes"),
    owner_entity_type: str = Form("individual"),
    owner_full_name: str = Form(""),
    owner_identifier: str = Form(""),
    owner_citizenship: str = Form(""),
    owner_phone: str = Form(""),
    owner_email: str = Form(""),
    date_of_birth: str = Form(""),
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    submitted_contacts = {
        "contact_email": contact_email,
        "contact_telegram": contact_telegram,
        "contact_phone": contact_phone,
        "contact_max": contact_max,
        "contact_other": contact_other,
    }
    driver_form = {
        "driver_same_as_policyholder": driver_same_as_policyholder,
        "driver_full_name": driver_full_name,
        "driver_identifier": driver_identifier,
        "driver_citizenship": driver_citizenship,
        "driver_phone": driver_phone,
        "driver_email": driver_email,
    }
    owner_form = {
        "owner_same_as_policyholder": owner_same_as_policyholder,
        "owner_entity_type": owner_entity_type,
        "owner_full_name": owner_full_name,
        "owner_identifier": owner_identifier,
        "owner_citizenship": owner_citizenship,
        "owner_phone": owner_phone,
        "owner_email": owner_email,
    }

    clean_name, name_error = validate_full_name(full_name)
    clean_identification_number, id_error = validate_identification_number(
        identification_number, field_label="Идентификационный номер"
    )
    clean_citizenship, citizenship_error = validate_citizenship(citizenship)
    clean_contacts, errors = validate_contacts_form(submitted_contacts)
    driver_clean, driver_errors = validate_driver_form(driver_form)
    owner_clean, owner_errors = validate_owner_form(owner_form)
    errors.update(driver_errors)
    errors.update(owner_errors)
    if name_error:
        errors["full_name"] = name_error
    if id_error:
        errors["identification_number"] = id_error
    if citizenship_error:
        errors["citizenship"] = citizenship_error

    clean_dob = None
    if _requires_date_of_birth(order.country_code):
        clean_dob, dob_error = validate_date_of_birth(date_of_birth, today=today_in_georgia())
        if dob_error:
            errors["date_of_birth"] = dob_error

    if errors:
        return render(
            request,
            "policyholder.html",
            _policyholder_context(
                errors=errors,
                full_name=full_name,
                identification_number=identification_number,
                citizenship=citizenship,
                contacts=submitted_contacts,
                driver=_driver_context_from_submission(driver_form),
                owner=_owner_context_from_submission(owner_form),
                steps=build_order_steps(order, 5),
                form_action=f"/o/{resume_token}/edit-policyholder",
                back_url=f"/o/{resume_token}/summary",
                submit_label="Сохранить",
                country_code=order.country_code,
                date_of_birth=date_of_birth,
            ),
            status_code=422,
        )

    update_policyholder(
        conn,
        order.id,
        full_name=clean_name,
        identification_number=clean_identification_number,
        citizenship=clean_citizenship,
        contact_email=clean_contacts["contact_email"],
        contact_telegram=clean_contacts["contact_telegram"],
        contact_phone=clean_contacts["contact_phone"],
        contact_max=clean_contacts["contact_max"],
        contact_other=clean_contacts["contact_other"],
        date_of_birth=clean_dob,
        **driver_clean,
        **owner_clean,
    )
    return _redirect(f"/o/{resume_token}/summary")
