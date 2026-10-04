"""Manual-payment state changes, shared by every transport (web customer
"Я оплатил", web /admin/orders, the Telegram bot's receipts and manager
buttons). The state machine itself stays in app.orders.state_machine; this
module only performs its payment transitions, each as a compare-and-set
(app.orders.repository.transition_if), so a double click, a replayed
callback or two managers at once can never apply one twice.

For a Telegram-channel order, the resulting Telegram sends (customer
notice, manager card updates, the manager review request) are enqueued in
the SAME transaction as the state change (app.notifications.bot_outbox);
the bot process delivers them. Nothing here talks to Telegram, so it is
safe to call from a FastAPI request thread.

    DATA_COMPLETED  --mark_awaiting_payment-->  AWAITING_PAYMENT
    AWAITING_PAYMENT --submit_payment_claim-->  PAYMENT_REVIEW   (receipt / "Я оплатил")
    PAYMENT_REVIEW  --confirm_payment------->   PAID
    PAYMENT_REVIEW  --reject_payment-------->   AWAITING_PAYMENT ("Оплата не найдена")
"""

import sqlite3
from dataclasses import dataclass, field

from app.analytics.repository import log_event
from app.notifications import bot_outbox
from app.orders.models import Order
from app.orders.repository import get_order_by_id, transition_if
from app.orders.state_machine import OrderStatus


@dataclass(frozen=True)
class PaymentAction:
    changed: bool  # False = the order wasn't in the expected state; nothing happened
    order: Order  # the order as it is AFTER the call
    history_id: int | None = None
    outbox_job_ids: list[int] = field(default_factory=list)


def _is_telegram(order: Order) -> bool:
    return order.channel == "telegram" and bool(order.bot_key) and order.telegram_chat_id is not None


def _load(conn: sqlite3.Connection, order_id: int) -> Order:
    order = get_order_by_id(conn, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    return order


def mark_awaiting_payment(conn: sqlite3.Connection, order_id: int, *, note: str) -> PaymentAction:
    history_id = transition_if(conn, order_id, OrderStatus.DATA_COMPLETED, OrderStatus.AWAITING_PAYMENT, note=note)
    return PaymentAction(changed=history_id is not None, order=_load(conn, order_id), history_id=history_id)


def mark_operator_issuance(conn: sqlite3.Connection, order_id: int, *, actor: str) -> PaymentAction:
    """DATA_COMPLETED -> PAID for an OPERATOR order only (payment_mode
    'operator': a manager issues the policy for a customer and handled the
    payment outside the bot). The history note states plainly that no
    customer payment was collected or verified here -- this is never a
    substitute for confirm_payment on a customer order."""
    order = _load(conn, order_id)
    if not order.is_operator_order:
        raise ValueError("mark_operator_issuance is only for operator orders")
    history_id = transition_if(
        conn, order_id, OrderStatus.DATA_COMPLETED, OrderStatus.PAID,
        note=f"operator issuance by {actor}: customer payment collection bypassed (no receipt, no payment check)",
    )
    return PaymentAction(changed=history_id is not None, order=_load(conn, order_id), history_id=history_id)


def submit_payment_claim(
    conn: sqlite3.Connection,
    order_id: int,
    *,
    note: str,
    manager_ids=(),
    file_row_ids: list[int] | None = None,
) -> PaymentAction:
    """The customer says they paid (web button, or a Telegram receipt). A
    claim alone never confirms anything -- it only puts the order in front
    of a human. For a Telegram order, the managers' review request (card +
    documents + receipts) is enqueued with the transition."""
    history_id = transition_if(
        conn, order_id, OrderStatus.AWAITING_PAYMENT, OrderStatus.PAYMENT_REVIEW, note=note, commit=False
    )
    if history_id is None:
        conn.rollback()
        return PaymentAction(changed=False, order=_load(conn, order_id))
    order = _load(conn, order_id)
    jobs: list[int] = []
    if _is_telegram(order) and manager_ids:
        jobs = bot_outbox.enqueue_review_request(
            conn, order, review_round=history_id, manager_ids=manager_ids, file_row_ids=file_row_ids or []
        )
    conn.commit()
    return PaymentAction(changed=True, order=order, history_id=history_id, outbox_job_ids=jobs)


def order_event_properties(order: Order) -> dict:
    """The only order facts funnel analytics may carry -- no personal or
    document data (see the bot_* events)."""
    return {
        "order_id": order.id,
        "bot_key": order.bot_key,
        "acquisition_source": order.acquisition_source,
        "category": order.vehicle_category_code,
        "period": order.period_code,
        "amount_rub": order.price_customer_minor // 100 if order.price_customer_minor is not None else None,
    }


def _decide(
    conn: sqlite3.Connection, order_id: int, *, to_status: OrderStatus, note: str, customer_kind: str, event_name: str, via: str
) -> PaymentAction:
    history_id = transition_if(conn, order_id, OrderStatus.PAYMENT_REVIEW, to_status, note=note, commit=False)
    if history_id is None:
        conn.rollback()
        return PaymentAction(changed=False, order=_load(conn, order_id))
    order = _load(conn, order_id)
    jobs: list[int] = []
    if _is_telegram(order):
        customer_job = bot_outbox.enqueue_customer_notice(conn, order, kind=customer_kind, history_id=history_id)
        jobs = ([customer_job] if customer_job else []) + bot_outbox.enqueue_card_updates(conn, order, history_id=history_id)
        log_event(
            conn,
            session_id=order.session_id,
            order_id=order.id,
            event_name=event_name,
            properties={**order_event_properties(order), "via": via},
        )  # commits -- together with the transition and the outbox jobs
    conn.commit()
    return PaymentAction(changed=True, order=order, history_id=history_id, outbox_job_ids=jobs)


def confirm_payment(conn: sqlite3.Connection, order_id: int, *, actor: str) -> PaymentAction:
    """PAYMENT_REVIEW -> PAID. actor: a non-personal label for the history
    note ("web admin 'x'", "telegram manager 123")."""
    return _decide(
        conn,
        order_id,
        to_status=OrderStatus.PAID,
        note=f"payment confirmed by {actor}",
        customer_kind=bot_outbox.KIND_CUSTOMER_PAYMENT_CONFIRMED,
        event_name="bot_payment_confirmed",
        via=actor.split(" ")[0],
    )


def reject_payment(conn: sqlite3.Connection, order_id: int, *, actor: str) -> PaymentAction:
    """PAYMENT_REVIEW -> AWAITING_PAYMENT ("Оплата не найдена"). Receipts
    already attached stay attached (audit trail); a new receipt starts a
    new review round."""
    return _decide(
        conn,
        order_id,
        to_status=OrderStatus.AWAITING_PAYMENT,
        note=f"payment not found ({actor})",
        customer_kind=bot_outbox.KIND_CUSTOMER_PAYMENT_REJECTED,
        event_name="bot_payment_rejected",
        via=actor.split(" ")[0],
    )
