"""Per-update customer context, built once by CustomerContextMiddleware so
every handler gets the same things the same way: the DB connection, the
settings, the bot profile, the customer's own draft session, and the OCR
dependencies (injected -- tests pass fakes, production builds real ones)."""

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import TelegramObject

from app.analytics.repository import log_event
from app.ocr.orientation import OrientationDetector
from app.ocr.provider import OcrProvider
from app.sessions.repository import get_draft, merge_draft
from app.settings import Settings
from app.telegram_bot import staff
from app.telegram_bot.profile import BotProfile
from app.telegram_bot.sessions import ensure_customer_session


@dataclass
class Ctx:
    conn: sqlite3.Connection
    settings: Settings
    profile: BotProfile
    bot: Bot
    session_id: str
    user_id: int
    chat_id: int
    ocr_provider: OcrProvider | None
    orientation_detector: OrientationDetector | None
    manager_ids: frozenset[int] = frozenset()
    outbox: Any = None  # app.telegram_bot.outbox.OutboxWorker
    _draft: dict | None = field(default=None, repr=False)

    def draft(self) -> dict:
        return get_draft(self.conn, self.session_id) or {}

    def merge(self, updates: dict) -> dict:
        return merge_draft(self.conn, self.session_id, updates)

    async def deliver(self, job_ids: list[int]) -> None:
        """Send the given outbox jobs right away (they stay queued for the
        background retry loop if a send fails)."""
        if self.outbox is not None and job_ids:
            await self.outbox.process(self.bot, self.conn, job_ids=job_ids)

    def event(self, name: str, **properties) -> None:
        """Funnel analytics -- callers pass only non-personal properties
        (counts, codes, booleans), never document values."""
        log_event(self.conn, session_id=self.session_id, order_id=None, event_name=name, properties=properties or None)


class CustomerContextMiddleware(BaseMiddleware):
    """Private chats only: groups/channels never get a customer session."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is not None and chat is not None and chat.type == ChatType.PRIVATE:
            conn = data["conn"]
            profile: BotProfile = data["profile"]
            session_id = ensure_customer_session(
                conn, profile, telegram_user_id=user.id, telegram_chat_id=chat.id, telegram_username=user.username
            )
            data["ctx"] = Ctx(
                conn=conn,
                settings=data["settings"],
                profile=profile,
                bot=data["bot"],
                session_id=session_id,
                user_id=user.id,
                chat_id=chat.id,
                ocr_provider=data.get("ocr_provider"),
                orientation_detector=data.get("orientation_detector"),
                # who verifies payments right now -- always from the DB
                manager_ids=staff.active_ids(conn, profile.bot_key),
                outbox=data.get("outbox"),
            )
        return await handler(event, data)
