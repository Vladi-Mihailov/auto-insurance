"""Admin-only routes: order visibility + manual RUB payment review
(/admin/orders).

Every route here depends on require_admin (HTTP Basic; fails closed with
401 if ADMIN_USERNAME/ADMIN_PASSWORD aren't both configured -- see
app.deps.require_admin). Deliberately narrow to the manual payment
confirmation MVP -- no other admin functionality exists.
"""

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.catalog import repository as catalog_repo
from app.deps import get_db, get_order_or_404, get_settings, require_admin
from app.notifications.telegram import notify_operator_order_paid
from app.orders.models import Order
from app.orders.repository import get_latest_transition_at, list_orders_by_status, set_status
from app.orders.state_machine import OrderStatus
from app.pricing.provider import get_period
from app.web.templating import render

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

# Every status an operator should be able to see on /admin/orders, in the
# order a real order actually moves through them (see
# app.orders.state_machine.ALLOWED_TRANSITIONS). DRAFT/CANCELLED/COMPLETED
# are deliberately excluded -- DRAFT has no Order row yet (see
# app.web.checkout_routes.post_policyholder, where the row is first
# created already at DATA_COMPLETED), and CANCELLED/COMPLETED aren't asked
# for here. PAYMENT_REVIEW stays the only status with action buttons (see
# admin_orders.html) -- it's still the one place a human decision
# (confirm/reject) actually happens; the others are read-only visibility.
_VISIBLE_STATUSES = [
    OrderStatus.DATA_COMPLETED,
    OrderStatus.AWAITING_PAYMENT,
    OrderStatus.PAYMENT_REVIEW,
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
    OrderStatus.POLICY_READY,
]

_STATUS_LABELS = {
    OrderStatus.DATA_COMPLETED: "Заявка заполнена",
    OrderStatus.AWAITING_PAYMENT: "Ожидает оплаты",
    OrderStatus.PAYMENT_REVIEW: "Оплата на проверке",
    OrderStatus.PAID: "Оплачено",
    OrderStatus.PROCESSING: "В обработке",
    OrderStatus.POLICY_READY: "Полис готов",
}


@router.get("/orders")
def get_admin_orders(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    groups = []
    for status in _VISIBLE_STATUSES:
        orders = list_orders_by_status(conn, status)
        rows = []
        for order in orders:
            category_name = order.vehicle_category_code
            if order.vehicle_category_code:
                category = catalog_repo.get_category_by_code(conn, order.vehicle_category_code)
                category_name = category.name if category else order.vehicle_category_code
            # "«Я оплатил» отправлено" only makes sense for the
            # payment-review action state -- other groups never queried
            # this transition at all, same as before this change.
            submitted_at = None
            if status == OrderStatus.PAYMENT_REVIEW:
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
        groups.append({"status": status.value, "label": _STATUS_LABELS[status], "rows": rows})

    return render(
        request,
        "admin_orders.html",
        {"groups": groups, "telegram_notify_failed": request.query_params.get("telegram_notify_failed") == "1"},
    )


@router.post("/orders/{resume_token}/confirm")
def post_admin_confirm_payment(
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    """"Подтвердить оплату" -- only acts if the order is still actually
    PAYMENT_REVIEW. Already-PAID (double click, stale tab, two admins) is a
    safe no-op: it must never attempt a second PAID transition, and the
    state machine has no PAID->PAID transition to even try. This is also
    exactly what keeps the operator Telegram notification below to at most
    one send per real transition: it only ever runs inside this same
    PAYMENT_REVIEW-gated branch, right after set_status succeeds, so a
    resubmit/double-click that finds the order already PAID skips both.

    The notification is intentionally best-effort (see
    app.notifications.telegram.notify_operator_order_paid): its failure
    must never roll back or fail this request -- the payment is already
    confirmed by the time it runs. A failure only redirects with a query
    flag so the admin list can surface it (see get_admin_orders).
    """
    if order.status == OrderStatus.PAYMENT_REVIEW.value:
        set_status(conn, order.id, OrderStatus.PAID, note="admin confirmed payment")

        settings = get_settings()
        category_name = order.vehicle_category_code
        if order.vehicle_category_code:
            category = catalog_repo.get_category_by_code(conn, order.vehicle_category_code)
            category_name = category.name if category else order.vehicle_category_code
        period_label = order.period_code
        if order.vehicle_category_code and order.period_code:
            period = get_period(settings, order.country_code, order.vehicle_category_code, order.period_code)
            if period:
                period_label = period.label

        notified = notify_operator_order_paid(
            api_id=settings.telegram_operator.api_id,
            api_hash=settings.telegram_operator.api_hash,
            phone=settings.telegram_operator.phone,
            session_path=settings.telegram_operator.session_path,
            chat_id=settings.telegram_operator.chat_id,
            order=order,
            category_name=category_name,
            period_label=period_label,
        )
        if not notified:
            return RedirectResponse("/admin/orders?telegram_notify_failed=1", status_code=303)
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
