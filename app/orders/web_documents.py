"""Website-uploaded document photos: private disk storage, the checkout
draft's references to them, attaching them to the created order, and the
staff notification (order card + each document) through the SAME outbox the
Telegram bots use (app.notifications.bot_outbox).

Lifecycle:

    POST /documents-soon  -> originals saved (save_upload), referenced from
                             the draft (DRAFT_KEY); a new successful batch
                             replaces -- and deletes -- the previous one
    POST /policyholder    -> order created, then attach_and_notify: one
                             insurance_order_files row per document (kind
                             vehicle_document, source 'web') + outbox jobs,
                             committed together; only then is the draft
                             cleared (by the caller)
    bot process           -> delivers the jobs (app.telegram_bot.outbox),
                             uploading each file from disk once

Storage: settings.app.uploads_dir (next to the DB by default, never under
app/web/static and never served by any route), directory 0700, files 0600,
names generated here (uuid) -- the customer's own file name is display
metadata only. Bytes are never logged, never put in an exception message;
nothing here logs OCR output. Abandoned uploads are removed by
cleanup_storage after settings.app.web_upload_orphan_hours; stored order
documents are kept unless settings.app.web_document_retention_days is set.
"""

import hashlib
import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.notifications import bot_outbox
from app.orders import files as order_files
from app.orders.models import Order
from app.sessions.repository import get_draft, merge_draft
from app.settings import Settings

logger = logging.getLogger(__name__)

DRAFT_KEY = "web_document_files"
SOURCE_WEB = "web"
_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
DEFAULT_COUNTRY_BOT = "GE"  # a country without its own bot (Armenia today) is delivered by Georgia's


# ---------------------------------------------------------------- storage


def _storage_dir(settings: Settings) -> Path:
    directory = settings.app.uploads_dir
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # e.g. a filesystem without POSIX modes (local Windows dev)
    return directory


def _path(settings: Settings, stored_name: str) -> Path | None:
    """The stored file for a system-generated name -- None for anything that
    isn't one (a tampered draft can never point outside the directory)."""
    if not stored_name or Path(stored_name).name != stored_name or stored_name.startswith("."):
        return None
    return settings.app.uploads_dir / stored_name


def save_upload(settings: Settings, data: bytes, *, mime_type: str, original_filename: str | None) -> dict:
    """Writes one already-validated upload (see app.ocr.image) and returns
    its draft metadata. Exclusive create with 0600 -- never overwrites."""
    stored_name = f"{uuid.uuid4().hex}{_EXTENSIONS.get(mime_type, '.bin')}"
    path = _storage_dir(settings) / stored_name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return {
        "stored_name": stored_name,
        "mime_type": mime_type,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "original_filename": (original_filename or "")[:200] or None,
    }


def read_stored(settings: Settings, stored_name: str | None) -> bytes | None:
    path = _path(settings, stored_name) if stored_name else None
    if path is None or not path.is_file():
        return None
    return path.read_bytes()


def delete_stored(settings: Settings, stored_names) -> None:
    for name in stored_names:
        path = _path(settings, name)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not delete a stored web upload: %s", type(exc).__name__)


# ------------------------------------------------------------------ draft


def replace_draft_documents(conn: sqlite3.Connection, settings: Settings, session_id: str, saved: list[dict]) -> None:
    """The batch that was just recognized becomes the draft's documents
    (positions 1..n, upload order); the previous batch's files are deleted --
    a re-upload never leaves two sets behind."""
    previous = (get_draft(conn, session_id) or {}).get(DRAFT_KEY) or []
    merge_draft(conn, session_id, {DRAFT_KEY: [{**meta, "position": n} for n, meta in enumerate(saved, start=1)]})
    keep = {meta["stored_name"] for meta in saved}
    delete_stored(settings, [m.get("stored_name") for m in previous if m.get("stored_name") not in keep])


# ---------------------------------------------------- order + notification


def delivery_bot_key(settings: Settings, country_code: str) -> str | None:
    """The bot whose staff receive a website order: that country's own bot,
    else Georgia's (Armenia has no bot). None if no bot is configured."""
    profiles = settings.telegram_bot_profiles
    for code in (country_code, DEFAULT_COUNTRY_BOT):
        for bot_key, profile in profiles.items():
            if profile.country_code == code:
                return bot_key
    return None


def attach_and_notify(conn: sqlite3.Connection, settings: Settings, order: Order, draft: dict) -> list[int]:
    """Attaches the draft's stored documents to the just-created website
    order and queues the staff notification (card + each document per
    active staff member of the delivery bot) -- ONE transaction, idempotent
    (UNIQUE file rows, outbox dedupe keys). Never raises: a failure is
    logged by class name and leaves the order valid without documents (the
    files stay on disk for cleanup_storage). Returns the outbox job ids."""
    from app.telegram_bot import staff  # plain SQLite module (no aiogram)

    bot_key = delivery_bot_key(settings, order.country_code)
    documents = draft.get(DRAFT_KEY) or []
    try:
        file_row_ids = []
        for meta in documents:
            if read_stored(settings, meta.get("stored_name")) is None:
                logger.warning("Web document missing from storage for order %s; skipped", order.public_number)
                continue
            order_files.add_file(
                conn,
                order_id=order.id,
                kind=order_files.KIND_VEHICLE_DOCUMENT,
                bot_key=bot_key or "",
                telegram_file_id="",  # set by the bot after its first upload
                telegram_file_unique_id=f"web:{meta['sha256']}",
                mime_type=meta.get("mime_type"),
                file_size=meta.get("size"),
                telegram_media_type="document",
                commit=False,
                storage_path=meta["stored_name"],
                source=SOURCE_WEB,
                position=meta.get("position"),
                original_filename=meta.get("original_filename"),
            )
        file_row_ids = [f.id for f in order_files.list_files(conn, order.id, order_files.KIND_VEHICLE_DOCUMENT)]
        jobs: list[int] = []
        if bot_key:
            manager_ids = staff.active_ids(conn, bot_key)
            if manager_ids:
                jobs = bot_outbox.enqueue_application(
                    conn, order, manager_ids=manager_ids, file_row_ids=file_row_ids, bot_key=bot_key
                )
        conn.commit()
        return jobs
    except Exception as exc:  # noqa: BLE001 -- never break checkout
        conn.rollback()
        logger.warning("Attaching web documents failed for order %s: %s", order.public_number, type(exc).__name__)
        return []


# ---------------------------------------------------------------- cleanup


def cleanup_storage(conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None) -> int:
    """Deletes (1) stored uploads no order references that are older than
    web_upload_orphan_hours (abandoned or failed checkouts), and (2) when
    web_document_retention_days is set, web order documents past it (the
    row stays; storage_path becomes NULL). Returns how many files were
    removed. Best-effort: never raises."""
    now = now or datetime.now(UTC)
    directory = settings.app.uploads_dir
    removed = 0
    try:
        if not directory.is_dir():
            return 0
        referenced = {
            row[0] for row in conn.execute("SELECT storage_path FROM insurance_order_files WHERE storage_path IS NOT NULL")
        }
        orphan_cutoff = (now - timedelta(hours=settings.app.web_upload_orphan_hours)).timestamp()
        for path in directory.iterdir():
            if path.is_file() and path.name not in referenced and path.stat().st_mtime < orphan_cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        days = settings.app.web_document_retention_days
        if days is not None:
            cutoff = (now - timedelta(days=days)).isoformat()
            rows = conn.execute(
                """SELECT f.id, f.storage_path FROM insurance_order_files f JOIN insurance_orders o ON o.id = f.order_id
                   WHERE f.source = ? AND f.storage_path IS NOT NULL AND o.created_at < ?""",
                (SOURCE_WEB, cutoff),
            ).fetchall()
            for row_id, stored_name in rows:
                delete_stored(settings, [stored_name])
                conn.execute("UPDATE insurance_order_files SET storage_path = NULL WHERE id = ?", (row_id,))
                removed += 1
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Web upload cleanup failed: %s", type(exc).__name__)
    return removed
