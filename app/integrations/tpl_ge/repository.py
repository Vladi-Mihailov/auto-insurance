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


def mark_bog_link_ready(conn: sqlite3.Connection, order_id: int, *, bog_payment_url: str, tpl_o_id: str | None) -> None:
    """tpl_o_id is overwritten every call -- a repeat BOG-link refresh mints
    a genuinely new o.id (confirmed via real HAR evidence: two separate
    /ecommerce/bog calls for the same tpl_uid returned two DIFFERENT o.id
    values), so the latest one is always what a later policy retrieval
    must use."""
    conn.execute(
        """
        UPDATE insurance_tpl_issuance
        SET issuance_status = ?, bog_payment_url = ?, tpl_o_id = ?, last_error = NULL, updated_at = ?
        WHERE order_id = ?
        """,
        (IssuanceStatus.BOG_LINK_READY.value, bog_payment_url, tpl_o_id, _now(), order_id),
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


def mark_policy_retrieved(
    conn: sqlite3.Connection,
    order_id: int,
    *,
    policy_number: str,
    tpl_policy_id: int | None,
    policy_document_url: str | None,
    invoice_document_url: str | None,
    additional_terms_document_url: str | None,
) -> None:
    """Persists a confirmed-issued policy's data -- called exactly once per
    successful GET /api/policies/{o.id} (see service.retrieve_issued_policy,
    which checks TplIssuance.is_policy_retrieved before ever calling this
    again). All fields set together, atomically, in one UPDATE -- a single
    row per order, so there is structurally no way for a retry to create a
    second/duplicate policy or document record. Deliberately does NOT touch
    issuance_status -- see IssuanceStatus's own docstring for why this
    stays an orthogonal concept."""
    conn.execute(
        """
        UPDATE insurance_tpl_issuance
        SET policy_number = ?, tpl_policy_id = ?, policy_document_url = ?,
            invoice_document_url = ?, additional_terms_document_url = ?,
            policy_retrieved_at = ?, last_error = NULL, updated_at = ?
        WHERE order_id = ?
        """,
        (
            policy_number,
            tpl_policy_id,
            policy_document_url,
            invoice_document_url,
            additional_terms_document_url,
            _now(),
            _now(),
            order_id,
        ),
    )
    conn.commit()


def mark_policy_sent_to_operator(conn: sqlite3.Connection, order_id: int) -> None:
    """Set exactly once, right after app.notifications.telegram.
    notify_operator_policy_ready actually confirms the send -- see
    app.web.admin_routes' delivery helper, the only caller. This is the
    entire duplicate-send guard: a repeat admin click checks
    TplIssuance.is_sent_to_operator before ever attempting delivery again."""
    conn.execute(
        "UPDATE insurance_tpl_issuance SET policy_sent_to_operator_at = ?, last_error = NULL, updated_at = ? WHERE order_id = ?",
        (_now(), _now(), order_id),
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
