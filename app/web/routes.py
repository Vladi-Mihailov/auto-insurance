"""Landing, resume, legacy post-order period/date, summary and payment.

The pre-order wizard (category+period -> dates -> upload/manual -> vehicle
details -> policyholder) lives in app/web/checkout_routes.py — split out so
this file doesn't grow into one giant module. This file keeps:

- landing / start (start now hands off into the checkout_routes wizard)
- the /o/{token} resume entrypoint
- /o/{token}/period and /o/{token}/date — LEGACY. New orders always get
  period_code/dates set at creation time (see create_order), so new checkout
  traffic never reaches these two routes. They stay only so any pre-existing
  order created under the old flow (period_code in {14d,1m,2m,3m}, or
  data_completed with no period/dates yet) keeps resuming correctly instead
  of 404ing. Do not link to them from new code.
- summary / payment
"""

import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.analytics.repository import log_event
from app.catalog import repository as catalog_repo
from app.dates.rules import GeorgiaDateRule, UnknownPeriodCode
from app.deps import get_db, get_order_or_404, get_session_id, get_settings
from app.orders.models import Order
from app.orders.repository import set_dates, set_period, set_status
from app.orders.state_machine import OrderStatus
from app.pricing.provider import available_periods, get_period
from app.web.step_nav import build_order_steps
from app.web.templating import render

router = APIRouter()

DATE_RULE = GeorgiaDateRule()


def _resume_redirect(order: Order) -> RedirectResponse:
    base = f"/o/{order.resume_token}"
    if not order.period_code:
        return RedirectResponse(f"{base}/period", status_code=303)  # legacy path only
    if not order.start_date or not order.end_date:
        return RedirectResponse(f"{base}/date", status_code=303)  # legacy path only
    if order.status == OrderStatus.DATA_COMPLETED.value:
        return RedirectResponse(f"{base}/summary", status_code=303)
    return RedirectResponse(f"{base}/payment", status_code=303)


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


@router.get("/")
def landing(request: Request, session_id: str = Depends(get_session_id), conn: sqlite3.Connection = Depends(get_db)):
    log_event(conn, session_id=session_id, order_id=None, event_name="landing_view")
    settings = get_settings()
    ge_periods = available_periods(settings, "GE", "passenger_car")
    priced = [p.price_rub for p in ge_periods if p.is_priced]
    ge_price_rub = min(priced) if priced else None
    return render(
        request,
        "landing.html",
        {"ge_price_rub": ge_price_rub, "contacts": settings.contacts},
    )


@router.get("/start")
def start(session_id: str = Depends(get_session_id), conn: sqlite3.Connection = Depends(get_db)):
    log_event(conn, session_id=session_id, order_id=None, event_name="checkout_started")
    return RedirectResponse("/category-period", status_code=303)


# ---------------------------------------------------------------------------
# Resume entrypoint
# ---------------------------------------------------------------------------


@router.get("/o/{resume_token}")
def resume(order: Order = Depends(get_order_or_404)):
    return _resume_redirect(order)


# ---------------------------------------------------------------------------
# Legacy period/date — see module docstring. Unchanged from before this
# iteration; kept only for orders created under the previous flow.
# ---------------------------------------------------------------------------


@router.get("/o/{resume_token}/period")
def get_period_screen(request: Request, order: Order = Depends(get_order_or_404)):
    periods = available_periods(get_settings(), order.country_code, "passenger_car")
    return render(
        request,
        "period.html",
        {"order": order, "periods": periods, "selected": order.period_code, "current_step": 3},
    )


@router.post("/o/{resume_token}/period")
def post_period(
    request: Request,
    resume_token: str,
    period_code: str = Form(...),
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    settings = get_settings()
    period = get_period(settings, order.country_code, "passenger_car", period_code)
    if period is None or not period.is_priced:
        periods = available_periods(settings, order.country_code, "passenger_car")
        return render(
            request,
            "period.html",
            {
                "order": order,
                "periods": periods,
                "selected": None,
                "error": "Выберите один из доступных периодов",
                "current_step": 3,
            },
            status_code=422,
        )

    set_period(conn, order.id, period_code=period.code, price_customer_minor=period.price_minor)
    return RedirectResponse(f"/o/{resume_token}/date", status_code=303)


@router.get("/o/{resume_token}/date")
def get_date_screen(request: Request, order: Order = Depends(get_order_or_404)):
    if not order.period_code:
        return RedirectResponse(f"/o/{order.resume_token}/period", status_code=303)

    preview_end = order.end_date
    if order.start_date and preview_end is None:
        preview_end = DATE_RULE.compute_end_date(order.start_date, order.period_code)

    return render(
        request,
        "date.html",
        {"order": order, "start_date": order.start_date, "end_date": preview_end, "current_step": 4},
    )


@router.get("/o/{resume_token}/date-preview")
def get_date_preview(start: str, order: Order = Depends(get_order_or_404)):
    if not order.period_code:
        raise HTTPException(status_code=400, detail="Period not selected yet")
    try:
        start_date = date.fromisoformat(start)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")

    try:
        end_date = DATE_RULE.compute_end_date(start_date, order.period_code)
    except UnknownPeriodCode as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return JSONResponse({"end_date": end_date.isoformat()})


@router.post("/o/{resume_token}/date")
def post_date(
    request: Request,
    resume_token: str,
    start_date: str = Form(...),
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    if not order.period_code:
        return RedirectResponse(f"/o/{resume_token}/period", status_code=303)

    try:
        parsed_start = date.fromisoformat(start_date)
    except ValueError:
        return render(
            request,
            "date.html",
            {"order": order, "start_date": None, "end_date": None, "error": "Некорректная дата", "current_step": 4},
            status_code=422,
        )

    end_date = DATE_RULE.compute_end_date(parsed_start, order.period_code)
    set_dates(conn, order.id, start_date=parsed_start, end_date=end_date)
    return RedirectResponse(f"/o/{resume_token}/summary", status_code=303)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@router.get("/o/{resume_token}/summary")
def get_summary(request: Request, order: Order = Depends(get_order_or_404), conn: sqlite3.Connection = Depends(get_db)):
    if not order.period_code:
        return RedirectResponse(f"/o/{order.resume_token}/period", status_code=303)
    if not order.start_date:
        return RedirectResponse(f"/o/{order.resume_token}/date", status_code=303)

    category_name = None
    if order.vehicle_category_code:
        category = catalog_repo.get_category_by_code(conn, order.vehicle_category_code)
        category_name = category.name if category else order.vehicle_category_code

    period_label = order.period_code
    if order.vehicle_category_code:
        period = get_period(get_settings(), order.country_code, order.vehicle_category_code, order.period_code)
        if period:
            period_label = period.label

    # vehicle_make/vehicle_model are a point-in-time SNAPSHOT taken at order
    # creation/edit (see app.orders.repository.create_order), not a live
    # join to the catalog — a later manufacturer/model rename or
    # deactivation must not silently change what this order says it's for.
    # (Old orders from before this snapshot existed still have these two
    # columns as their only source for these names anyway, so the same read
    # works for both.)
    manufacturer_name = order.vehicle_make
    model_name = order.vehicle_model

    return render(
        request,
        "summary.html",
        {
            "order": order,
            "category_name": category_name,
            "period_label": period_label,
            "manufacturer_name": manufacturer_name,
            "model_name": model_name,
            "steps": build_order_steps(order, 6),
        },
    )


@router.post("/o/{resume_token}/summary")
def post_summary(
    resume_token: str,
    action: str = Form(...),
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    if action == "edit":
        return RedirectResponse(f"/o/{resume_token}/edit-vehicle", status_code=303)

    set_status(conn, order.id, OrderStatus.AWAITING_PAYMENT, note="client confirmed summary")
    log_event(conn, session_id=order.session_id, order_id=order.id, event_name="payment_screen_viewed")
    return RedirectResponse(f"/o/{resume_token}/payment", status_code=303)


# ---------------------------------------------------------------------------
# Payment — manual RUB transfer + admin-reviewed confirmation (see
# app.web.admin_routes for the admin side). No automatic verification: this
# route only ever records that the customer CLAIMS to have paid, never an
# amount -- price is always read from the order, never from this request.
# ---------------------------------------------------------------------------


@router.get("/o/{resume_token}/payment")
def get_payment(request: Request, order: Order = Depends(get_order_or_404)):
    settings = get_settings()
    # Payment is beyond the 6-step checkout wizard (Транспорт и срок ... Проверка)
    # -- no progress nav here, matching the step list the wizard actually has.
    # This single page also doubles as the post-payment status page (see
    # payment.html's branching on order.status) -- _resume_redirect above
    # already sends any order past DATA_COMPLETED here regardless of status.
    return render(request, "payment.html", {"order": order, "payment": settings.payment})


@router.post("/o/{resume_token}/confirm-payment")
def post_confirm_payment(
    resume_token: str,
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Customer clicks "Я оплатил". Carries no amount or other payment data
    at all -- the only thing this reports is the fact of a claim, which is
    exactly why the form in payment.html has no fields.

    Idempotent by construction: only transitions AWAITING_PAYMENT ->
    PAYMENT_REVIEW when the order is actually still AWAITING_PAYMENT. A
    double-click, a resubmit, or hitting this after an admin already acted
    (PAYMENT_REVIEW/PAID/anything else) just falls through to the same
    redirect with no state change and no error -- the state machine
    (ensure_transition_allowed, via set_status) remains the authoritative
    enforcement layer; this check exists so an already-completed action
    never surfaces as a 500 to the customer.
    """
    if order.status == OrderStatus.AWAITING_PAYMENT.value:
        set_status(
            conn,
            order.id,
            OrderStatus.PAYMENT_REVIEW,
            note="customer submitted payment confirmation",
        )
    return RedirectResponse(f"/o/{resume_token}/payment", status_code=303)
