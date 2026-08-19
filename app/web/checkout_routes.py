"""The pre-order checkout wizard: category+period -> dates -> upload/manual
choice -> vehicle details -> policyholder (creates the order).

Split out of app/web/routes.py (which keeps landing/resume/legacy period-date/
summary/payment) so that file doesn't grow into a single giant module — this
one owns everything that happens before an order exists, plus the small JSON
catalog endpoints the vehicle-details picker calls.
"""

import sqlite3
import time
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from app.analytics.repository import log_event
from app.catalog import repository as catalog_repo
from app.catalog.sync import sync_models_on_demand
from app.countries import COUNTRIES, match_citizenship_text
from app.dates.rules import GeorgiaDateRule, UnknownPeriodCode, today_in_georgia
from app.deps import get_db, get_ocr_provider, get_order_or_404, get_session_id, get_settings
from app.ocr.image import MAX_FILES_PER_RECOGNITION, UploadValidationError, validate_and_normalize_upload
from app.ocr.parser import build_candidates
from app.ocr.provider import OcrProvider, OcrProviderError
from app.orders.models import Order
from app.orders.repository import create_order, set_dates, update_coverage, update_policyholder, update_vehicle_fields
from app.pricing.provider import available_periods, get_period
from app.sessions.repository import clear_draft, get_draft, merge_draft
from app.validation import (
    validate_citizenship,
    validate_contacts_form,
    validate_driver_form,
    validate_full_name,
    validate_identification_number,
    validate_owner_form,
    validate_vehicle_details_form,
)
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
    # A fresh checkout (draft has no start_date yet) defaults the FIELD
    # DISPLAY to today in Georgia -- never UTC/the browser's own timezone,
    # and never written into the draft here; it only becomes the chosen
    # value once the user actually submits the form. Once a start_date
    # exists in the draft (the user already chose one, even if it was
    # today), it's shown as-is on every later visit -- this default never
    # overwrites a real choice on back/forward navigation.
    effective_start = date.fromisoformat(start_value) if start_value else today_in_georgia()
    end_value = DATE_RULE.compute_end_date(effective_start, draft["period_code"])
    return render(
        request,
        "date_step.html",
        {
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

    today = today_in_georgia()
    parsed_start, error = _parse_and_validate_start_date(start_date, today)
    if error:
        return render(
            request,
            "date_step.html",
            {
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
        },
    )
    return _redirect("/vehicle")


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
                values={
                    **form,
                    "manufacturer_id": manufacturer_id,
                    "model_id": model_id,
                    "data_entry_method": draft.get("data_entry_method"),
                    "ocr_manufacturer_hint": draft.get("ocr_manufacturer_hint"),
                    "ocr_model_hint": draft.get("ocr_model_hint"),
                },
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
    session_id: str = Depends(get_session_id),
    conn: sqlite3.Connection = Depends(get_db),
):
    draft = get_draft(conn, session_id)
    if not _require_draft_keys(draft, ("manufacturer_id", "model_id")):
        return _redirect("/vehicle")

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
        identification_number=clean_identification_number,
        citizenship=clean_citizenship,
        contact_email=clean_contacts["contact_email"],
        contact_telegram=clean_contacts["contact_telegram"],
        contact_phone=clean_contacts["contact_phone"],
        contact_max=clean_contacts["contact_max"],
        contact_other=clean_contacts["contact_other"],
        customer_currency="RUB",
        purchase_currency="GEL",
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
            "min_date": today_in_georgia().isoformat(),
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
    today = today_in_georgia()
    parsed_start, error = _parse_and_validate_start_date(start_date, today)
    if error:
        return render(
            request,
            "date_step.html",
            {
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

    new_end_date = DATE_RULE.compute_end_date(parsed_start, order.period_code)
    set_dates(conn, order.id, start_date=parsed_start, end_date=new_end_date)
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
        **driver_clean,
        **owner_clean,
    )
    return _redirect(f"/o/{resume_token}/summary")
