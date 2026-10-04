"""Drains app.notifications.bot_outbox -- the ONLY place Telegram sends for
manager cards, manager files and customer payment notices happen.

Called right after an action (so delivery is immediate) and from a
background loop in the bot process (retries with backoff, and jobs enqueued
by the web admin). Each job re-reads the order from the DB and renders the
message from its CURRENT state, so a late or retried send never shows
stale information. A failed send never touches order state: the job is
simply retried (up to bot_outbox.MAX_ATTEMPTS), and only the exception
class is logged -- never a Telegram response body or personal data.
"""

import asyncio
import logging
import sqlite3

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import ReplyParameters

from app.notifications import bot_outbox
from app.orders import files as order_files
from app.orders.repository import get_order_by_id
from app.settings import Settings
from app.telegram_bot import order_views, staff, texts
from app.telegram_bot.managers import card_keyboard, card_text
from app.telegram_bot.profile import BotProfile

logger = logging.getLogger(__name__)

_MANAGER_KINDS = {bot_outbox.KIND_MANAGER_CARD, bot_outbox.KIND_MANAGER_FILE, bot_outbox.KIND_MANAGER_CARD_UPDATE}

_WAIT_FOR_CARD_SECONDS = 20


class _Postpone(Exception):
    pass


class OutboxWorker:
    def __init__(self, settings: Settings, profile: BotProfile):
        self._settings = settings
        self._profile = profile

    async def process(self, bot: Bot, conn: sqlite3.Connection, *, job_ids: list[int] | None = None) -> int:
        """Runs due jobs (all of them, or just job_ids). Returns how many were sent."""
        sent = 0
        for job in bot_outbox.due_jobs(conn, self._profile.bot_key, job_ids=job_ids):
            if await self._run(bot, conn, job):
                sent += 1
        return sent

    async def _run(self, bot: Bot, conn: sqlite3.Connection, job: bot_outbox.OutboxJob) -> bool:
        order = get_order_by_id(conn, job.order_id)
        if order is None or order.bot_key != self._profile.bot_key:
            bot_outbox.mark_sent(conn, job.id)  # nothing sensible to deliver
            return False
        try:
            await self._send(bot, conn, job, order)
        except _Postpone:
            bot_outbox.postpone(conn, job, delay_seconds=_WAIT_FOR_CARD_SECONDS)
            return False
        except TelegramRetryAfter as exc:
            bot_outbox.mark_retry(conn, job, error_class=type(exc).__name__, delay_seconds=exc.retry_after)
            return False
        except Exception as exc:  # noqa: BLE001 -- any send failure: retry later, never crash the loop
            status = bot_outbox.mark_retry(conn, job, error_class=type(exc).__name__)
            logger.warning("Outbox job %s (%s, order %s) failed: %s -> %s", job.id, job.kind, order.public_number, type(exc).__name__, status)
            return False
        bot_outbox.mark_sent(conn, job.id)
        return True

    async def _send(self, bot: Bot, conn: sqlite3.Connection, job: bot_outbox.OutboxJob, order) -> None:
        if job.kind in _MANAGER_KINDS and not staff.is_staff(conn, order.bot_key, job.target_chat_id):
            # Queued for someone who has been removed from the staff since:
            # order data never goes to them (the job is closed, not retried).
            logger.info("Outbox job %s skipped: recipient is no longer staff", job.id)
            return
        if job.kind == bot_outbox.KIND_MANAGER_CARD:
            message = await bot.send_message(
                job.target_chat_id,
                card_text(conn, self._settings, order),
                reply_markup=card_keyboard(conn, order, auto_issuance=self._settings.telegram_bot.tpl_auto_issuance),
            )
            bot_outbox.upsert_manager_message(
                conn,
                order_id=order.id,
                manager_user_id=job.target_chat_id,
                chat_id=job.target_chat_id,
                message_id=message.message_id,
                purpose=bot_outbox.PURPOSE_ORDER_CARD,
            )
            return

        if job.kind == bot_outbox.KIND_MANAGER_FILE:
            await self._send_manager_file(bot, conn, job, order)
            return

        if job.kind == bot_outbox.KIND_MANAGER_CARD_UPDATE:
            tracked = bot_outbox.get_manager_message(conn, order.id, job.payload.get("manager_user_id"))
            if tracked is None:
                return
            try:
                await bot.edit_message_text(
                    card_text(conn, self._settings, order),
                    chat_id=tracked.chat_id,
                    message_id=tracked.message_id,
                    reply_markup=card_keyboard(conn, order, auto_issuance=self._settings.telegram_bot.tpl_auto_issuance),
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc):
                    raise
            return

        # Customer notices: ALWAYS the chat stored on the order.
        if job.kind == bot_outbox.KIND_CUSTOMER_PAYMENT_CONFIRMED:
            await bot.send_message(
                order.telegram_chat_id,
                texts.PAYMENT_CONFIRMED.format(number=order.public_number),
                reply_markup=order_views.order_keyboard(conn, order),
            )
            return
        if job.kind == bot_outbox.KIND_CUSTOMER_PAYMENT_REJECTED:
            await bot.send_message(
                order.telegram_chat_id,
                order_views.rejected_text(self._settings, order),
                reply_markup=order_views.order_keyboard(conn, order),
            )
            return
        raise ValueError(f"unknown outbox job kind {job.kind!r}")

    async def _send_manager_file(self, bot: Bot, conn: sqlite3.Connection, job: bot_outbox.OutboxJob, order) -> None:
        file = order_files.get_file(conn, job.payload["file_row_id"])
        if file is None or file.order_id != order.id:
            return
        card = bot_outbox.get_manager_message(conn, order.id, job.target_chat_id)
        if card is None:
            # The card goes first; if it's still queued, wait for it.
            pending_card = any(
                j.kind == bot_outbox.KIND_MANAGER_CARD and j.target_chat_id == job.target_chat_id and j.status == "pending"
                for j in bot_outbox.list_jobs(conn, order.id)
            )
            if pending_card:
                raise _Postpone()
        caption_key = "extra_receipt" if job.payload.get("extra") else file.kind
        caption = texts.MGR_FILE_CAPTIONS.get(caption_key, "{number}").format(
            number=order.public_number,
            amount=texts.format_rub(order.price_customer_minor // 100),
            name=order.full_name or "—",
        )
        reply = ReplyParameters(message_id=card.message_id, allow_sending_without_reply=True) if card else None
        if file.telegram_media_type == "photo":
            await bot.send_photo(job.target_chat_id, file.telegram_file_id, caption=caption, reply_parameters=reply)
        else:
            await bot.send_document(job.target_chat_id, file.telegram_file_id, caption=caption, reply_parameters=reply)


async def run_outbox_loop(bot: Bot, worker: OutboxWorker, db_file, *, interval_seconds: float = 15.0) -> None:
    """Background delivery/retry loop for the bot process."""
    from app.db import get_connection

    while True:
        conn = get_connection(db_file)
        try:
            await worker.process(bot, conn)
        except Exception as exc:  # noqa: BLE001 -- keep the loop alive
            logger.warning("Outbox loop iteration failed: %s", type(exc).__name__)
        finally:
            conn.close()
        await asyncio.sleep(interval_seconds)

