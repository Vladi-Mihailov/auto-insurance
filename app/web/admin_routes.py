"""Admin-only routes: manual RUB payment review (/admin/orders).

Every route here depends on require_admin (HTTP Basic; fails closed with
401 if ADMIN_USERNAME/ADMIN_PASSWORD aren't both configured -- see
app.deps.require_admin). Deliberately narrow to the manual payment
confirmation MVP -- no other admin functionality exists.
"""

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.catalog import repository as catalog_repo
from app.deps import get_db, get_order_or_404, require_admin
from app.orders.models import Order
from app.orders.repository import get_latest_transition_at, list_orders_by_status, set_status
from app.orders.state_machine import OrderStatus
from app.web.templating import render

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@router.get("/orders")
def get_admin_orders(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    orders = list_orders_by_status(conn, OrderStatus.PAYMENT_REVIEW)

    rows = []
    for order in orders:
        category_name = order.vehicle_category_code
        if order.vehicle_category_code:
            category = catalog_repo.get_category_by_code(conn, order.vehicle_category_code)
            category_name = category.name if category else order.vehicle_category_code
        submitted_at = get_latest_transition_at(
            conn,
            order.id,
            from_status=OrderStatus.AWAITING_PAYMENT,
            to_status=OrderStatus.PAYMENT_REVIEW,
        )
        rows.append(
            {
                "order": order,
                "category_name": category_name,
                "submitted_at": submitted_at,
            }
        )

    return render(request, "admin_orders.html", {"rows": rows})


@router.post("/orders/{resume_token}/confirm")
def post_admin_confirm_payment(
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    """"Подтвердить оплату" -- only acts if the order is still actually
    PAYMENT_REVIEW. Already-PAID (double click, stale tab, two admins) is a
    safe no-op: it must never attempt a second PAID transition, and the
    state machine has no PAID->PAID transition to even try."""
    if order.status == OrderStatus.PAYMENT_REVIEW.value:
        set_status(conn, order.id, OrderStatus.PAID, note="admin confirmed payment")
    return RedirectResponse("/admin/orders", status_code=303)


@router.post("/orders/{resume_token}/reject")
def post_admin_reject_payment(
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    """"Оплата не найдена" -- only acts if the order is still actually
    PAYMENT_REVIEW. Critically, an already-PAID order is left untouched:
    this must never roll a confirmed payment back to AWAITING_PAYMENT, even
    from a stale admin page loaded before someone else already confirmed
    it."""
    if order.status == OrderStatus.PAYMENT_REVIEW.value:
        set_status(conn, order.id, OrderStatus.AWAITING_PAYMENT, note="admin payment not found")
    return RedirectResponse("/admin/orders", status_code=303)
