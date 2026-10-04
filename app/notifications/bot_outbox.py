"""Durable Telegram outbox + per-manager message tracking (plain SQLite, no
aiogram import -- the web app enqueues through this too).

Whoever changes a Telegram order's state (the bot on a manager tap, or the
web admin) enqueues the resulting Telegram sends in the SAME transaction as
the state change; the bot process is the only thing that ever talks to
Telegram, draining due jobs (app.telegram_bot.outbox). Every job has a
dedupe_key, so enqueueing is idempotent: a replayed tap, a retried request
or a restart can't produce a second card or a second customer message.
Payloads carry only routing data (file row ids, review round ids) --
never names, documents, phone numbers or Telegram file ids.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.orders.models import Order

KIND_MANAGER_CARD = "manager_card"  # send the order card to one manager
KIND_MANAGER_FILE = "manager_file"  # send one vehicle document / receipt to one manager
KIND_MANAGER_CARD_UPDATE = "manager_card_update"  # re-render one manager's tracked card
KIND_CUSTOMER_PAYMENT_CONFIRMED = "customer_payment_confirmed"
KIND_CUSTOMER_PAYMENT_REJECTED = "customer_payment_rejected"

PURPOSE_ORDER_CARD = "order_card"

MAX_ATTEMPTS = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OutboxJob:
    id: int
    bot_key: str
    dedupe_key: str
    kind: str
    order_id: int
    target_chat_id: int
    payload: dict
    status: str
    attempts: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OutboxJob":
        return cls(
            id=row["id"],
            bot_key=row["bot_key"],
            dedupe_key=row["dedupe_key"],
            kind=row["kind"],
            order_id=row["order_id"],
            target_chat_id=row["target_chat_id"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
            status=row["status"],
            attempts=row["attempts"],
        )


def enqueue(
    conn: sqlite3.Connection,
    *,
    bot_key: str,
    dedupe_key: str,
    kind: str,
    order_id: int,
    target_chat_id: int,
    payload: dict | None = None,
) -> int | None:
    """New job id, or None if a job with this dedupe_key already exists.
    Never commits -- part of the caller's transaction."""
    now = _now().isoformat()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO telegram_outbox
               (bot_key, dedupe_key, kind, order_id, target_chat_id, payload, status, attempts,
                next_attempt_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
        (bot_key, dedupe_key, kind, order_id, target_chat_id, json.dumps(payload) if payload else None, now, now, now),
    )
    return cursor.lastrowid if cursor.rowcount == 1 else None


def due_jobs(conn: sqlite3.Connection, bot_key: str, *, limit: int = 50, job_ids: list[int] | None = None) -> list[OutboxJob]:
    params: list = [bot_key, _now().isoformat()]
    query = "SELECT * FROM telegram_outbox WHERE bot_key = ? AND status = 'pending' AND next_attempt_at <= ?"
    if job_ids is not None:
        if not job_ids:
            return []
        query += f" AND id IN ({','.join('?' for _ in job_ids)})"
        params.extend(job_ids)
    query += " ORDER BY id LIMIT ?"
    params.append(limit)
    return [OutboxJob.from_row(row) for row in conn.execute(query, params).fetchall()]


def mark_sent(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE telegram_outbox SET status = 'sent', updated_at = ?, last_error = NULL WHERE id = ?",
        (_now().isoformat(), job_id),
    )
    conn.commit()


def mark_retry(conn: sqlite3.Connection, job: OutboxJob, *, error_class: str, delay_seconds: float | None = None) -> str:
    """Schedules another attempt with exponential backoff, or gives up after
    MAX_ATTEMPTS (status 'failed'). error_class is an exception class name
    only -- never a Telegram response body. Returns the new status."""
    attempts = job.attempts + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    delay = delay_seconds if delay_seconds is not None else min(30 * 2 ** (attempts - 1), 3600)
    conn.execute(
        """UPDATE telegram_outbox SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
           WHERE id = ?""",
        (status, attempts, (_now() + timedelta(seconds=delay)).isoformat(), error_class[:100], _now().isoformat(), job.id),
    )
    conn.commit()
    return status


def postpone(conn: sqlite3.Connection, job: OutboxJob, *, delay_seconds: float) -> None:
    """Try again later without counting an attempt (e.g. a receipt waiting
    for its order card to be sent first)."""
    conn.execute(
        "UPDATE telegram_outbox SET next_attempt_at = ?, updated_at = ? WHERE id = ?",
        ((_now() + timedelta(seconds=delay_seconds)).isoformat(), _now().isoformat(), job.id),
    )
    conn.commit()


def list_jobs(conn: sqlite3.Connection, order_id: int) -> list[OutboxJob]:
    rows = conn.execute("SELECT * FROM telegram_outbox WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()
    return [OutboxJob.from_row(row) for row in rows]


# ------------------------------------------------------ manager messages


@dataclass(frozen=True)
class ManagerMessage:
    order_id: int
    manager_user_id: int
    chat_id: int
    message_id: int
    purpose: str


def upsert_manager_message(
    conn: sqlite3.Connection, *, order_id: int, manager_user_id: int, chat_id: int, message_id: int, purpose: str
) -> None:
    now = _now().isoformat()
    conn.execute(
        """INSERT INTO telegram_manager_messages
               (order_id, manager_user_id, chat_id, message_id, purpose, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (order_id, manager_user_id, purpose) DO UPDATE SET
               chat_id = excluded.chat_id, message_id = excluded.message_id, updated_at = excluded.updated_at""",
        (order_id, manager_user_id, chat_id, message_id, purpose, now, now),
    )
    conn.commit()


def manager_messages(conn: sqlite3.Connection, order_id: int, purpose: str = PURPOSE_ORDER_CARD) -> list[ManagerMessage]:
    rows = conn.execute(
        """SELECT order_id, manager_user_id, chat_id, message_id, purpose FROM telegram_manager_messages
           WHERE order_id = ? AND purpose = ? ORDER BY id""",
        (order_id, purpose),
    ).fetchall()
    return [ManagerMessage(**dict(row)) for row in rows]


def get_manager_message(
    conn: sqlite3.Connection, order_id: int, manager_user_id: int, purpose: str = PURPOSE_ORDER_CARD
) -> ManagerMessage | None:
    for message in manager_messages(conn, order_id, purpose):
        if message.manager_user_id == manager_user_id:
            return message
    return None


# ------------------------------------------------------ what to enqueue


def current_review_round(conn: sqlite3.Connection, order_id: int) -> int | None:
    """History row id of the latest AWAITING_PAYMENT -> PAYMENT_REVIEW
    transition: identifies one "please check this payment" round."""
    row = conn.execute(
        """SELECT id FROM insurance_order_status_history
           WHERE order_id = ? AND from_status = 'awaiting_payment' AND to_status = 'payment_review'
           ORDER BY id DESC LIMIT 1""",
        (order_id,),
    ).fetchone()
    return row["id"] if row else None


def enqueue_review_request(
    conn: sqlite3.Connection, order: Order, *, review_round: int, manager_ids, file_row_ids: list[int]
) -> list[int]:
    """Per manager: the order card, then every attached vehicle document and
    receipt (each its own job -- a failed file send is retried alone, never
    re-sending the card)."""
    job_ids = []
    for manager_id in sorted(manager_ids):
        job_ids.append(
            enqueue(
                conn,
                bot_key=order.bot_key,
                dedupe_key=f"card:{order.id}:{review_round}:{manager_id}",
                kind=KIND_MANAGER_CARD,
                order_id=order.id,
                target_chat_id=manager_id,
                payload={"round": review_round},
            )
        )
        job_ids.extend(
            enqueue_manager_file(conn, order, review_round=review_round, manager_id=manager_id, file_row_id=file_row_id)
            for file_row_id in file_row_ids
        )
    return [job_id for job_id in job_ids if job_id]


def enqueue_manager_file(conn: sqlite3.Connection, order: Order, *, review_round: int, manager_id: int, file_row_id: int, extra: bool = False):
    return enqueue(
        conn,
        bot_key=order.bot_key,
        # Same key whether it was queued with the card or later as an extra
        # receipt -- so it can never be delivered twice.
        dedupe_key=f"file:{order.id}:{review_round}:{file_row_id}:{manager_id}",
        kind=KIND_MANAGER_FILE,
        order_id=order.id,
        target_chat_id=manager_id,
        payload={"round": review_round, "file_row_id": file_row_id, "extra": extra},
    )


def enqueue_card_updates(conn: sqlite3.Connection, order: Order, *, history_id: int) -> list[int]:
    job_ids = []
    for message in manager_messages(conn, order.id):
        job_ids.append(
            enqueue(
                conn,
                bot_key=order.bot_key,
                dedupe_key=f"cardupd:{order.id}:{history_id}:{message.manager_user_id}",
                kind=KIND_MANAGER_CARD_UPDATE,
                order_id=order.id,
                target_chat_id=message.chat_id,
                payload={"manager_user_id": message.manager_user_id},
            )
        )
    return [job_id for job_id in job_ids if job_id]


def enqueue_customer_notice(conn: sqlite3.Connection, order: Order, *, kind: str, history_id: int) -> int | None:
    return enqueue(
        conn,
        bot_key=order.bot_key,
        dedupe_key=f"{kind}:{order.id}:{history_id}",
        kind=kind,
        order_id=order.id,
        # Always the chat stored on the order -- never anything else.
        target_chat_id=order.telegram_chat_id,
    )
