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
from app.integrations.tpl_ge import repository as tpl_ge_repo
from app.integrations.tpl_ge import service as tpl_ge_service
from app.integrations.tpl_ge.errors import TplIssuanceError
from app.notifications.telegram import notify_operator_order_paid, notify_operator_policy_ready
from app.orders.models import Order
from app.orders.repository import get_latest_transition_at, list_orders_by_status, set_status
from app.orders.state_machine import OrderStatus
from app.pricing.provider import get_period
from app.web.templating import render

# Statuses a GE order's TPL-issuance card can appear under -- it starts
# showing right after PAID and keeps showing through PROCESSING (issuance in
# progress), same status range app.integrations.tpl_ge.service.issue_tpl_policy
# itself accepts. Never shown for AM/TR (see get_admin_orders below) or for
# any other status.
_TPL_ISSUANCE_VISIBLE_STATUSES = {OrderStatus.PAID.value, OrderStatus.PROCESSING.value}

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
            # GE-only, PAID/PROCESSING-only -- AM/TR orders and every other
            # status never get a tpl_issuance value at all (stays None),
            # which is what keeps admin_orders.html from rendering this
            # block for them (see the template's country_code check).
            tpl_issuance = None
            if order.country_code == "GE" and order.status in _TPL_ISSUANCE_VISIBLE_STATUSES:
                tpl_issuance = tpl_ge_repo.get_issuance_by_order_id(conn, order.id)
            rows.append(
                {
                    "order": order,
                    "category_name": category_name,
                    "submitted_at": submitted_at,
                    "tpl_issuance": tpl_issuance,
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


@router.post("/orders/{resume_token}/tpl/issue")
def post_admin_tpl_issue(
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    """"Оформить полис TPL" (first click) AND "Получить новую ссылку" (any
    later click) -- the same idempotent action, see
    app.integrations.tpl_ge.service.issue_tpl_policy's own docstring for
    why one endpoint correctly serves both. Any TplIssuanceError is already
    persisted as insurance_tpl_issuance.last_error by issue_tpl_policy
    itself before it propagates -- the admin list reads that column
    directly (see admin_orders.html), so nothing further needs to happen
    with the exception here beyond not letting it become a 500."""
    settings = get_settings()
    try:
        tpl_ge_service.issue_tpl_policy(conn, order, settings)
    except TplIssuanceError:
        pass
    return RedirectResponse("/admin/orders", status_code=303)


@router.post("/orders/{resume_token}/tpl/mark-paid")
def post_admin_tpl_mark_paid(
    order: Order = Depends(get_order_or_404),
    conn: sqlite3.Connection = Depends(get_db),
):
    """"Оплата TPL завершена" (first click) AND "Получить полис повторно"
    (any later click) -- the same idempotent action, same one-endpoint-
    serves-both-labels pattern already used by post_admin_tpl_issue above.

    Sequence: record the operator's acknowledgement (idempotent -- see
    tpl_ge_service.report_operator_paid's own docstring), then, if the
    policy isn't already retrieved, attempt retrieval with a small bounded
    retry (tpl_ge_service.retrieve_issued_policy_with_retry -- never an
    unbounded loop, never re-initiates BOG/creates another application).
    Once retrieved, deliver the Policy PDF to the operator's Telegram chat
    exactly once (_deliver_policy_pdf below, guarded by
    TplIssuance.is_sent_to_operator) -- if that delivery fails, the
    already-persisted policy_number/policy_document_url are left intact
    for a later resend (this same route, clicked again).

    Order.status is never moved to POLICY_READY here (still unexplored --
    see the delivery report's OPEN ITEMS) -- only insurance_tpl_issuance's
    own fields change; the admin page reads those directly to show the
    issued policy number."""
    settings = get_settings()
    try:
        issuance = tpl_ge_service.report_operator_paid(conn, order)
    except TplIssuanceError:
        return RedirectResponse("/admin/orders", status_code=303)

    if not issuance.is_policy_retrieved:
        issuance = tpl_ge_service.retrieve_issued_policy_with_retry(conn, order, settings)

    if issuance.is_policy_retrieved and not issuance.is_sent_to_operator:
        _deliver_policy_pdf(conn, order, issuance, settings)

    return RedirectResponse("/admin/orders", status_code=303)


def _deliver_policy_pdf(conn, order: Order, issuance, settings) -> None:
    """Downloads ONLY the Policy PDF (never Invoice/Additional Terms) using
    the exact policy_document_url TPL's own API already provided, hands the
    bytes straight to Telegram, and releases them -- no temporary file, no
    permanent storage anywhere (see tpl_ge_service.download_policy_pdf's
    own docstring for why, and the delivery report's PDF STORAGE section).
    A failure at either step is recorded via tpl_ge_repo.record_error
    (never raised further, never rolls back the already-persisted
    policy_number/policy_document_url/policy_retrieved_at) -- the admin
    page surfaces last_error, and the SAME "Получить полис повторно"
    action retries delivery on a later click."""
    if not issuance.policy_document_url:
        tpl_ge_repo.record_error(conn, order.id, error_message="Policy document URL missing -- cannot deliver PDF")
        return

    try:
        pdf_bytes = tpl_ge_service.download_policy_pdf(issuance.policy_document_url)
    except Exception as exc:
        tpl_ge_repo.record_error(conn, order.id, error_message=f"Policy PDF download failed: {type(exc).__name__}")
        return

    sent = notify_operator_policy_ready(
        api_id=settings.telegram_operator.api_id,
        api_hash=settings.telegram_operator.api_hash,
        phone=settings.telegram_operator.phone,
        session_path=settings.telegram_operator.session_path,
        chat_id=settings.telegram_operator.chat_id,
        order=order,
        policy_number=issuance.policy_number,
        pdf_bytes=pdf_bytes,
    )
    if sent:
        tpl_ge_repo.mark_policy_sent_to_operator(conn, order.id)
    else:
        tpl_ge_repo.record_error(conn, order.id, error_message="Telegram PDF delivery failed")
