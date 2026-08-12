"""The pre-order checkout wizard: category+period -> dates -> upload/manual
choice -> vehicle details -> policyholder (creates the order).

Split out of app/web/routes.py (which keeps landing/resume/legacy period-date/
summary/payment) so that file doesn't grow into a single giant module — this
one owns everything that happens before an order exists, plus the small JSON
catalog endpoints the vehicle-details picker calls.
"""

import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.analytics.repository import log_event
from app.catalog import repository as catalog_repo
from app.catalog.sync import sync_models_on_demand
from app.dates.rules import GeorgiaDateRule, UnknownPeriodCode
from app.deps import get_db, get_order_or_404, get_session_id, get_settings
from app.orders.models import Order
from app.orders.repository import create_order, set_dates, update_coverage, update_policyholder, update_vehicle_fields
from app.pricing.provider import available_periods, get_period
from app.sessions.repository import clear_draft, get_draft, merge_draft
from app.validation import validate_contact, validate_full_name, validate_vehicle_details_form
from app.web.step_nav import build_draft_steps, build_order_steps
from app.web.templating import render

router = APIRouter()

DATE_RULE = GeorgiaDateRule()
COUNTRY_CODE = "GE"


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _require_draft_keys(draft: dict | None, keys: tuple[str, ...]) -> bool:
    if not draft:
        return False
    return all(draft.get(key) is not None for key in keys)


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
    categories = catalog_repo.list_categories(conn)
    selected_category = draft.get("vehicle_category_code") or "passenger_car"
    periods = available_periods(settings, COUNTRY_CODE, selected_category)
    # Default to the first priced period only when nothing has been chosen
    # yet — never overwrite a period the user (or an earlier draft) already
    # picked. Purely a display default: nothing is written to the draft
    # until the form is actually submitted.
    priced = [p for p in periods if p.is_priced]
    selected_period = draft.get("period_code") or (priced[0].code if priced else None)
    return render(
        request,
        "category_period.html",
        {
            "categories": categories,
            "selected_category": selected_category,
            "periods": periods,
            "selected_period": selected_period,
            "steps": build_draft_steps(draft, 1),
            "form_action": "/category-period",
            "back_url": "/",
            "submit_label": "Далее",
        },
    )


@router.get("/api/periods")
def get_periods_for_category(category_code: str):
    """Small JSON helper so the category tiles can refresh the period list
    without a full page reload when the user switches category — same
    JSONResponse pattern as the existing date-preview endpoint."""
    settings = get_settings()
    periods = available_periods(settings, COUNTRY_CODE, category_code)
    return JSONResponse(
        [{"code": p.code, "label": p.label, "price_rub": p.price_rub, "is_priced": p.is_priced} for p in periods]
    )


@router.post("/category-period")
def post_category_period(
    request: Request,
    category_code: str = Form(...),
    period_code: str = Form(...),
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id) or {}
    settings = get_settings()
    category = catalog_repo.get_category_by_code(conn, category_code)
    period = get_period(settings, COUNTRY_CODE, category_code, period_code) if category else None

    error = None
    if category is None:
        error = "Выберите категорию транспорта"
    elif period is None:
        error = "Выберите один из доступных периодов"
    elif not period.is_priced:
        error = "Цена для этого периода пока не настроена — оформление временно недоступно"

    if error:
        categories = catalog_repo.list_categories(conn)
        periods = available_periods(settings, COUNTRY_CODE, category_code)
        return render(
            request,
            "category_period.html",
            {
                "categories": categories,
                "selected_category": category_code,
                "periods": periods,
                "selected_period": None,
                "error": error,
                "steps": build_draft_steps(draft, 1),
                "form_action": "/category-period",
                "back_url": "/",
                "submit_label": "Далее",
            },
            status_code=422,
        )

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
        recomputed_end = DATE_RULE.compute_end_date(date.fromisoformat(draft["start_date"]), period.code)
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
    if not _require_draft_keys(draft, ("vehicle_category_code", "period_code")):
        return _redirect("/category-period")

    start_value = draft.get("start_date")
    # Recompute end_date from the CURRENT period rather than trusting the
    # stored value for display — belt-and-suspenders alongside the
    # invalidation in post_category_period above, in case a draft was ever
    # written by older code that didn't recompute it.
    end_value = (
        DATE_RULE.compute_end_date(date.fromisoformat(start_value), draft["period_code"]).isoformat()
        if start_value
        else None
    )
    return render(
        request,
        "date_step.html",
        {
            "start_date": date.fromisoformat(start_value) if start_value else None,
            "end_date": date.fromisoformat(end_value) if end_value else None,
            "steps": build_draft_steps(draft, 2),
            "form_action": "/date",
            "back_url": "/category-period",
            "submit_label": "Далее",
            "date_preview_url": "/api/date-preview",
        },
    )


@router.get("/api/date-preview")
def get_date_preview_preorder(
    start: str,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("period_code",)):
        raise HTTPException(status_code=400, detail="Period not selected yet")

    try:
        start_date = date.fromisoformat(start)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")

    try:
        end_date = DATE_RULE.compute_end_date(start_date, draft["period_code"])
    except UnknownPeriodCode as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return JSONResponse({"end_date": end_date.isoformat()})


@router.post("/date")
def post_date_step(
    request: Request,
    start_date: str = Form(...),
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("vehicle_category_code", "period_code")):
        return _redirect("/category-period")

    try:
        parsed_start = date.fromisoformat(start_date)
    except ValueError:
        return render(
            request,
            "date_step.html",
            {
                "start_date": None,
                "end_date": None,
                "error": "Некорректная дата",
                "steps": build_draft_steps(draft, 2),
                "form_action": "/date",
                "back_url": "/category-period",
                "submit_label": "Далее",
                "date_preview_url": "/api/date-preview",
            },
            status_code=422,
        )

    end_date = DATE_RULE.compute_end_date(parsed_start, draft["period_code"])
    merge_draft(conn, session_id, {"start_date": parsed_start.isoformat(), "end_date": end_date.isoformat()})
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


@router.get("/documents-soon")
def documents_soon(request: Request):
    return render(request, "documents_soon.html")


# ---------------------------------------------------------------------------
# Step 4 (manual path): vehicle details — registration number, VIN/chassis,
# manufacturer, model.
# ---------------------------------------------------------------------------


def _vehicle_form_context(
    *, values: dict, errors: dict, form_action: str, manufacturer_name: str | None, model_name: str | None, steps: list, back_url: str
) -> dict:
    return {
        "values": values,
        "errors": errors,
        "form_action": form_action,
        "manufacturer_name": manufacturer_name,
        "model_name": model_name,
        "steps": steps,
        "back_url": back_url,
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
    # "Заполнить вручную" on /method or via the "Заполнить вручную" fallback
    # link on /documents-soon — means manual entry, by definition. Checking
    # for the literal "manual" value here used to dead-end anyone who had
    # picked "Загрузить документы" and then used that fallback link, since
    # their draft still said data_entry_method="documents". "documents" and
    # "manual" are two ways to fill the same draft, not two different ones.
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("start_date", "end_date")):
        return _redirect("/date")
    if draft.get("data_entry_method") != "manual":
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
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("start_date", "end_date")):
        return _redirect("/date")

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

    if errors:
        return render(
            request,
            "vehicle_form.html",
            _vehicle_form_context(
                values={**form, "manufacturer_id": manufacturer_id, "model_id": model_id},
                errors=errors,
                form_action="/vehicle",
                manufacturer_name=manufacturer_name,
                model_name=None,
                steps=build_draft_steps(draft, 4),
                back_url="/method",
            ),
            status_code=422,
        )

    merge_draft(
        conn,
        session_id,
        {
            "data_entry_method": "manual",
            "registration_number": clean["registration_number"],
            "identifier_type": clean["identifier_type"],
            "identifier": clean["identifier"],
            "manufacturer_id": resolved_manufacturer_id,
            "model_id": resolved_model_id,
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


@router.get("/policyholder")
def get_policyholder(
    request: Request,
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("manufacturer_id", "model_id")):
        return _redirect("/vehicle")
    return render(
        request,
        "policyholder.html",
        {
            "errors": {},
            "full_name": "",
            "selected_type": "telegram",
            "contact_value": "",
            "steps": build_draft_steps(draft, 5),
            "form_action": "/policyholder",
            "back_url": "/vehicle",
            "submit_label": "Продолжить",
        },
    )


@router.post("/policyholder")
def post_policyholder(
    request: Request,
    full_name: str = Form(""),
    contact_type: str = Form(...),
    contact_value: str = Form(""),
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("manufacturer_id", "model_id")):
        return _redirect("/vehicle")

    clean_name, name_error = validate_full_name(full_name)
    clean_contact, contact_error = validate_contact(contact_type, contact_value)

    if name_error or contact_error:
        errors = {}
        if name_error:
            errors["full_name"] = name_error
        if contact_error:
            errors["contact_value"] = contact_error
        return render(
            request,
            "policyholder.html",
            {
                "errors": errors,
                "full_name": full_name,
                "selected_type": contact_type,
                "contact_value": contact_value,
                "steps": build_draft_steps(draft, 5),
                "form_action": "/policyholder",
                "back_url": "/vehicle",
                "submit_label": "Продолжить",
            },
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
        country_code=COUNTRY_CODE,
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
        contact_type=contact_type,
        contact_value=clean_contact,
        customer_currency="RUB",
        purchase_currency="GEL",
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
            },
            errors={},
            form_action=f"/o/{order.resume_token}/edit-vehicle",
            manufacturer_name=manufacturer_name,
            model_name=model_name,
            steps=build_order_steps(order, 4),
            back_url=f"/o/{order.resume_token}/summary",
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

    if errors:
        return render(
            request,
            "vehicle_form.html",
            _vehicle_form_context(
                values={**form, "manufacturer_id": manufacturer_id, "model_id": model_id},
                errors=errors,
                form_action=f"/o/{resume_token}/edit-vehicle",
                manufacturer_name=manufacturer_name,
                model_name=None,
                steps=build_order_steps(order, 4),
                back_url=f"/o/{resume_token}/summary",
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
    settings = get_settings()
    categories = catalog_repo.list_categories(conn)
    periods = available_periods(settings, order.country_code, order.vehicle_category_code)
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
    category = catalog_repo.get_category_by_code(conn, category_code)
    period = get_period(settings, order.country_code, category_code, period_code) if category else None

    error = None
    if category is None:
        error = "Выберите категорию транспорта"
    elif period is None:
        error = "Выберите один из доступных периодов"
    elif not period.is_priced:
        error = "Цена для этого периода пока не настроена — оформление временно недоступно"

    if error:
        categories = catalog_repo.list_categories(conn)
        periods = available_periods(settings, order.country_code, category_code)
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
    new_end_date = DATE_RULE.compute_end_date(order.start_date, period.code)
    set_dates(conn, order.id, start_date=order.start_date, end_date=new_end_date)
    return _redirect(f"/o/{resume_token}/summary")


@router.get("/o/{resume_token}/edit-date")
def get_edit_date(
    request: Request,
    order: Order = Depends(get_order_or_404),
):
    return render(
        request,
        "date_step.html",
        {
            "start_date": order.start_date,
            "end_date": order.end_date,
            "steps": build_order_steps(order, 2),
            "form_action": f"/o/{order.resume_token}/edit-date",
            "back_url": f"/o/{order.resume_token}/summary",
            "submit_label": "Сохранить",
            "date_preview_url": f"/o/{order.resume_token}/date-preview",
        },
    )


@router.post("/o/{resume_token}/edit-date")
def post_edit_date(
    request: Request,
    resume_token: str,
    start_date: str = Form(...),
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        parsed_start = date.fromisoformat(start_date)
    except ValueError:
        return render(
            request,
            "date_step.html",
            {
                "start_date": None,
                "end_date": None,
                "error": "Некорректная дата",
                "steps": build_order_steps(order, 2),
                "form_action": f"/o/{resume_token}/edit-date",
                "back_url": f"/o/{resume_token}/summary",
                "submit_label": "Сохранить",
                "date_preview_url": f"/o/{resume_token}/date-preview",
            },
            status_code=422,
        )

    new_end_date = DATE_RULE.compute_end_date(parsed_start, order.period_code)
    set_dates(conn, order.id, start_date=parsed_start, end_date=new_end_date)
    return _redirect(f"/o/{resume_token}/summary")


@router.get("/o/{resume_token}/edit-policyholder")
def get_edit_policyholder(
    request: Request,
    order: Order = Depends(get_order_or_404),
):
    return render(
        request,
        "policyholder.html",
        {
            "errors": {},
            "full_name": order.full_name,
            "selected_type": order.contact_type,
            "contact_value": order.contact_value,
            "steps": build_order_steps(order, 5),
            "form_action": f"/o/{order.resume_token}/edit-policyholder",
            "back_url": f"/o/{order.resume_token}/summary",
            "submit_label": "Сохранить",
        },
    )


@router.post("/o/{resume_token}/edit-policyholder")
def post_edit_policyholder(
    request: Request,
    resume_token: str,
    full_name: str = Form(""),
    contact_type: str = Form(...),
    contact_value: str = Form(""),
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    clean_name, name_error = validate_full_name(full_name)
    clean_contact, contact_error = validate_contact(contact_type, contact_value)

    if name_error or contact_error:
        errors = {}
        if name_error:
            errors["full_name"] = name_error
        if contact_error:
            errors["contact_value"] = contact_error
        return render(
            request,
            "policyholder.html",
            {
                "errors": errors,
                "full_name": full_name,
                "selected_type": contact_type,
                "contact_value": contact_value,
                "steps": build_order_steps(order, 5),
                "form_action": f"/o/{resume_token}/edit-policyholder",
                "back_url": f"/o/{resume_token}/summary",
                "submit_label": "Сохранить",
            },
            status_code=422,
        )

    update_policyholder(conn, order.id, full_name=clean_name, contact_type=contact_type, contact_value=clean_contact)
    return _redirect(f"/o/{resume_token}/summary")
