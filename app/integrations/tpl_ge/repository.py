"""Raw-SQL repository for insurance_tpl_issuance -- same conventions as
app/orders/repository.py (raw parameterized SQL, no ORM). One row per Order
that has ever started GE TPL issuance; order_id is UNIQUE, so
create_issuance is only ever called once per order (see
service.issue_tpl_policy, the only writer)."""

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from app.integrations.tpl_ge.models import IssuanceStatus, TplIssuance


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_issuance_by_order_id(conn: sqlite3.Connection, order_id: int) -> TplIssuance | None:
    row = conn.execute("SELECT * FROM insurance_tpl_issuance WHERE order_id = ?", (order_id,)).fetchone()
    return TplIssuance.from_row(row) if row else None


def create_issuance(conn: sqlite3.Connection, order_id: int, *, tpl_uid: str) -> TplIssuance:
    """Allocates the ONE tpl_uid this order will ever use. Never call this
    a second time for the same order_id -- the UNIQUE constraint on
    order_id (and on tpl_uid) makes a mistaken second call fail loudly
    rather than silently minting a second identity for the same order."""
    now = _now()
    conn.execute(
        """
        INSERT INTO insurance_tpl_issuance (order_id, tpl_uid, issuance_status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (order_id, tpl_uid, IssuanceStatus.PENDING.value, now, now),
    )
    conn.commit()
    issuance = get_issuance_by_order_id(conn, order_id)
    assert issuance is not None
    return issuance


def mark_application_created(
    conn: sqlite3.Connection, order_id: int, *, tpl_product_id: int, tpl_purchase_price_gel: Decimal
) -> None:
    conn.execute(
        """
        UPDATE insurance_tpl_issuance
        SET issuance_status = ?, tpl_product_id = ?, tpl_purchase_price_gel = ?, last_error = NULL, updated_at = ?
        WHERE order_id = ?
        """,
        (IssuanceStatus.APPLICATION_CREATED.value, tpl_product_id, str(tpl_purchase_price_gel), _now(), order_id),
    )
    conn.commit()


def mark_bog_link_ready(conn: sqlite3.Connection, order_id: int, *, bog_payment_url: str) -> None:
    conn.execute(
        """
        UPDATE insurance_tpl_issuance
        SET issuance_status = ?, bog_payment_url = ?, last_error = NULL, updated_at = ?
        WHERE order_id = ?
        """,
        (IssuanceStatus.BOG_LINK_READY.value, bog_payment_url, _now(), order_id),
    )
    conn.commit()


def mark_operator_reported_paid(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute(
        "UPDATE insurance_tpl_issuance SET issuance_status = ?, updated_at = ? WHERE order_id = ?",
        (IssuanceStatus.OPERATOR_REPORTED_PAID.value, _now(), order_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, order_id: int, *, error_message: str) -> None:
    """Only for a failure BEFORE the TPL application has ever been created
    (issuance still PENDING) -- moves issuance_status to FAILED, meaning
    "nothing created yet, safe to retry from scratch once the operator
    fixes the underlying data". See service.issue_tpl_policy for the guard
    that picks this vs. record_error below.

    Must NEVER be called once application_already_created is True -- doing
    so would erase the one fact (see TplIssuance.application_already_created)
    that keeps a later retry from re-sending a second, duplicate
    POST /api/policies. Use record_error for any failure after that point."""
    conn.execute(
        "UPDATE insurance_tpl_issuance SET issuance_status = ?, last_error = ?, updated_at = ? WHERE order_id = ?",
        (IssuanceStatus.FAILED.value, error_message, _now(), order_id),
    )
    conn.commit()


def record_error(conn: sqlite3.Connection, order_id: int, *, error_message: str) -> None:
    """A failure AFTER the TPL application already exists (e.g. a BOG
    handoff refresh attempt failed) -- records last_error without touching
    issuance_status, so application_already_created stays True and a later
    retry only repeats the BOG handoff step, never POST /api/policies."""
    conn.execute(
        "UPDATE insurance_tpl_issuance SET last_error = ?, updated_at = ? WHERE order_id = ?",
        (error_message, _now(), order_id),
    )
    conn.commit()
