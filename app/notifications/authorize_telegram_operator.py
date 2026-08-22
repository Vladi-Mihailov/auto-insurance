"""One-time interactive authorization for auto-insurance's OWN dedicated
Telegram user session (see app.notifications.telegram module docstring for
the full design).

Creates/authorizes ONLY TelegramOperatorSettings.session_path (default
data/sessions/auto_insurance_operator.session) -- this script never reads,
copies, or otherwise touches any ai-lead-radar session (reader_live/
reader_sync/reader_notifier/inviter sessions live entirely in that other
project's own directory; nothing here even references that path).

Same proven technique as ai-lead-radar's own
reader/notifications/authorize_notifier.py: Telethon's client.start()
prompts interactively (via stdin) for the Telegram login code, and for the
2FA password if the account has one enabled. The one difference from that
reference script: TELEGRAM_PHONE is passed in from settings (env) rather
than re-typed by hand, since the task this session was set up for already
requires that variable to be configured -- the phone number itself is
still never logged or written anywhere by this script beyond what
Telethon's own session file records.

Usage:
    python -m app.notifications.authorize_telegram_operator

Only creates/authorizes the session -- sends no message.
"""

import asyncio
import sys

from telethon import TelegramClient

from app.deps import get_settings


async def _run() -> None:
    settings = get_settings()
    telegram = settings.telegram_operator

    missing = [
        name
        for name, value in [
            ("TELEGRAM_API_ID", telegram.api_id),
            ("TELEGRAM_API_HASH", telegram.api_hash),
            ("TELEGRAM_PHONE", telegram.phone),
        ]
        if not value
    ]
    if missing:
        print(f"Missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        print("Set them in .env before running this command.", file=sys.stderr)
        sys.exit(1)

    session_path = telegram.session_path
    session_path.parent.mkdir(parents=True, exist_ok=True)

    print("Authorizing auto-insurance's own dedicated Telegram operator session.")
    print(f"Session will be created at: {session_path}.session")
    print("This is a NEW, independent session file -- it does not read, copy, or")
    print("share any ai-lead-radar session.")
    print()

    client = TelegramClient(str(session_path), telegram.api_id, telegram.api_hash)
    # Telethon itself prompts for the Telegram login code, and for the 2FA
    # password if the account has one enabled -- neither is read from env
    # or stored by this script beyond what the resulting session file holds.
    await client.start(phone=telegram.phone)
    await client.disconnect()

    print()
    print(f"Session created and authorized: {session_path}.session")
    print("No message was sent -- this command only creates/authorizes the session.")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)


if __name__ == "__main__":
    main()
