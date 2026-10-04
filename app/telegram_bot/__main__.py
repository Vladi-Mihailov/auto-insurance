"""python -m app.telegram_bot -- run the Telegram sales bot (long polling).

Reads config/config.yaml + .env exactly like the web app. Refuses to start
(exit code 2, message naming the missing/invalid variable, never a value)
unless TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_KEY and TELEGRAM_BOT_MANAGER_IDS
are valid."""

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot

from app.db import init_db
from app.settings import load_settings
from app.telegram_bot.app import build_dispatcher, install_secret_redaction
from app.telegram_bot.config import BotConfigError, load_bot_config
from app.telegram_bot.outbox import run_outbox_loop

PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("app.telegram_bot")


async def _run() -> int:
    settings = load_settings(PROJECT_ROOT)
    try:
        config = load_bot_config(settings)
    except BotConfigError as exc:
        logger.error("Bot not started: %s", exc)
        return 2

    token = config.token.get_secret_value()
    install_secret_redaction(token)
    init_db(settings.app.db_file)

    bot = Bot(token=token)
    dispatcher = build_dispatcher(settings=settings, config=config)
    try:
        me = await bot.get_me()
        logger.info(
            "Bot @%s starting (bot_key=%s, managers configured: %d, OCR: %s, orientation detector: %s)",
            me.username,
            config.profile.bot_key,
            len(config.manager_ids),
            "on" if dispatcher["ocr_provider"] is not None else "off (no OPENAI_API_KEY)",
            getattr(dispatcher["orientation_detector"], "name", "none"),
        )
        # Polling and a webhook are mutually exclusive on Telegram's side; a
        # reissued token may still have an old webhook registered.
        if not settings.telegram_payment.is_complete:
            logger.warning(
                "Telegram payment details incomplete (missing %s) -- customers will be told payment is temporarily unavailable",
                ", ".join(settings.telegram_payment.missing_variables()),
            )
        await bot.delete_webhook(drop_pending_updates=False)
        # Delivers queued manager/customer notifications (incl. ones enqueued
        # by the web admin) and retries failed sends.
        outbox_task = asyncio.create_task(run_outbox_loop(bot, dispatcher["outbox"], settings.app.db_file))
        try:
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
        finally:
            outbox_task.cancel()
    finally:
        await bot.session.close()
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
