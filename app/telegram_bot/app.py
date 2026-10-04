"""Dispatcher assembly -- shared by the real entry point (__main__) and the
offline tests, so tests exercise exactly the production wiring."""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Dispatcher
from aiogram.fsm.storage.base import BaseEventIsolation
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import TelegramObject

from app.db import get_connection, init_db
from app.ocr.orientation import build_orientation_detector
from app.ocr.provider import OcrProvider, build_ocr_provider
from app.settings import Settings
from app.telegram_bot import staff
from app.telegram_bot.albums import DEFAULT_DEBOUNCE_SECONDS, AlbumCollector
from app.telegram_bot.config import BotConfig
from app.telegram_bot.documents import DocumentsRuntime
from app.telegram_bot.context import CustomerContextMiddleware
from app.telegram_bot.handlers import build_customer_router, on_error
from app.telegram_bot.managers import build_manager_router
from app.telegram_bot.outbox import OutboxWorker
from app.telegram_bot.storage import SessionConversationStorage


class DbConnectionMiddleware(BaseMiddleware):
    """One SQLite connection per update, closed afterwards -- the same
    connection-per-request pattern as app.deps.get_db."""

    def __init__(self, db_file: Path):
        self._db_file = db_file

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        conn = get_connection(self._db_file)
        data["conn"] = conn
        try:
            return await handler(event, data)
        finally:
            conn.close()


_FROM_SETTINGS = object()


def default_ocr_provider(settings: Settings) -> OcrProvider | None:
    """Same rule as the web app (app.deps.get_ocr_provider): no key -> None,
    reported to the customer as unavailable -- never a hidden stub."""
    return build_ocr_provider(settings.ocr.openai_api_key, settings.ocr.vision_model)


def build_dispatcher(
    *,
    settings: Settings,
    config: BotConfig,
    events_isolation: BaseEventIsolation | None = None,
    ocr_provider=_FROM_SETTINGS,
    orientation_detector=_FROM_SETTINGS,
    album_debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
) -> Dispatcher:
    """ocr_provider / orientation_detector default to the ones settings
    configure; tests inject fakes (never a real OpenAI/Tesseract call)."""
    # Staff (managers/owner) live in the DB; the env configuration is synced
    # into it on every start (roles and invites survive restarts).
    init_db(settings.app.db_file)
    conn = get_connection(settings.app.db_file)
    try:
        staff.bootstrap(conn, config.profile.bot_key, config.manager_ids, owner_id=config.owner_id)
    finally:
        conn.close()
    storage = SessionConversationStorage(settings.app.db_file, config.profile.bot_key)
    isolation = events_isolation or SimpleEventIsolation()
    dispatcher = Dispatcher(
        storage=storage,
        # Serializes updates per (chat, user): a fast double tap can never
        # run two handlers against the same draft at once.
        events_isolation=isolation,
    )
    dispatcher["documents_runtime"] = DocumentsRuntime(
        collector=AlbumCollector(debounce_seconds=album_debounce_seconds),
        storage=storage,
        isolation=isolation,  # album finalization takes the same per-customer lock
        db_file=settings.app.db_file,
    )
    dispatcher["settings"] = settings
    dispatcher["profile"] = config.profile
    dispatcher["bot_config"] = config
    dispatcher["ocr_provider"] = default_ocr_provider(settings) if ocr_provider is _FROM_SETTINGS else ocr_provider
    dispatcher["orientation_detector"] = (
        build_orientation_detector(settings.ocr.orientation_detector, settings.ocr.tesseract_cmd)
        if orientation_detector is _FROM_SETTINGS
        else orientation_detector
    )
    dispatcher["outbox"] = OutboxWorker(settings, config.profile)
    dispatcher.update.outer_middleware(DbConnectionMiddleware(settings.app.db_file))
    dispatcher.update.outer_middleware(CustomerContextMiddleware())
    dispatcher.include_router(build_manager_router())  # first: manager actions + policy PDFs
    dispatcher.include_router(build_customer_router())
    dispatcher.errors.register(on_error)
    return dispatcher


class SecretRedactingFilter(logging.Filter):
    """Defense in depth: replaces the bot token wherever it might appear in
    a formatted log line (e.g. a library logging a request URL)."""

    def __init__(self, secret: str):
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secret:
            return True
        message = record.getMessage()
        if self._secret in message:
            record.msg = message.replace(self._secret, "[REDACTED]")
            record.args = None
        return True


def install_secret_redaction(secret: str) -> None:
    redactor = SecretRedactingFilter(secret)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
