"""insurance_order_files: Telegram file REFERENCES attached to an order
(vehicle documents, payment receipts, the policy PDF). Never file bytes.
file_id/file_unique_id are never logged anywhere."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

KIND_VEHICLE_DOCUMENT = "vehicle_document"
KIND_PAYMENT_RECEIPT = "payment_receipt"
KIND_POLICY = "policy"


@dataclass(frozen=True)
class OrderFile:
    id: int
    order_id: int
    kind: str
    bot_key: str
    telegram_file_id: str
    telegram_file_unique_id: str
    mime_type: str | None
    file_size: int | None
    telegram_media_type: str | None  # "photo" | "document"
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OrderFile":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


def add_file(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    kind: str,
    bot_key: str,
    telegram_file_id: str,
    telegram_file_unique_id: str,
    mime_type: str | None,
    file_size: int | None,
    telegram_media_type: str,
    commit: bool = True,
) -> int | None:
    """New row id, or None when this exact Telegram file (file_unique_id) is
    already attached to this order under this kind -- a duplicate upload is
    a no-op, never a second row."""
    cursor = conn.execute(
        """INSERT OR IGNORE INTO insurance_order_files
               (order_id, kind, bot_key, telegram_file_id, telegram_file_unique_id, mime_type, file_size,
                telegram_media_type, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            order_id,
            kind,
            bot_key,
            telegram_file_id,
            telegram_file_unique_id,
            mime_type,
            file_size,
            telegram_media_type,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    if commit:
        conn.commit()
    return cursor.lastrowid if cursor.rowcount == 1 else None


def list_files(conn: sqlite3.Connection, order_id: int, kind: str | None = None) -> list[OrderFile]:
    if kind is None:
        rows = conn.execute("SELECT * FROM insurance_order_files WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM insurance_order_files WHERE order_id = ? AND kind = ? ORDER BY id", (order_id, kind)
        ).fetchall()
    return [OrderFile.from_row(row) for row in rows]


def get_file(conn: sqlite3.Connection, file_row_id: int) -> OrderFile | None:
    row = conn.execute("SELECT * FROM insurance_order_files WHERE id = ?", (file_row_id,)).fetchone()
    return OrderFile.from_row(row) if row else None


def latest_file(conn: sqlite3.Connection, order_id: int, kind: str) -> OrderFile | None:
    files = list_files(conn, order_id, kind)
    return files[-1] if files else None
