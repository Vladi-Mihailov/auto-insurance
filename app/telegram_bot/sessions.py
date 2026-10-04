"""Telegram customer <-> insurance_sessions mapping, and /start attribution.

A Telegram customer's pre-order draft is an ordinary insurance_sessions row
(the same one the web checkout uses), keyed by a synthetic session id that
is unique per (bot, Telegram user) -- two users, or the same user on two
different bots, never share a draft.

Attribution rule (explicit, tested in tests/test_telegram_bot_start.py):
- /start <valid source> stores that source on the active draft; a later
  /start with a DIFFERENT valid source replaces it (last meaningful touch).
- A plain /start, or one whose payload is not a valid source, NEVER clears
  or changes an already-captured source -- it only leaves it as it is.
- The draft's source is what later becomes insurance_orders.
  acquisition_source; every /start is also logged as a "bot_started"
  analytics event (with the valid source or null) for the starts stage of
  the funnel. An invalid payload's raw value is never stored or logged.
"""

import re
import sqlite3
from dataclasses import dataclass

from app.analytics.repository import log_event
from app.sessions.repository import ensure_session, get_draft, merge_draft
from app.telegram_bot.profile import BotProfile

_SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def session_id_for(bot_key: str, telegram_user_id: int) -> str:
    return f"tg:{bot_key}:{telegram_user_id}"


def parse_source(payload: str | None) -> str | None:
    """The deep-link payload if it is a valid source, else None."""
    if payload is None:
        return None
    payload = payload.strip()
    return payload if _SOURCE_RE.fullmatch(payload) else None


@dataclass(frozen=True)
class StartResult:
    session_id: str
    acquisition_source: str | None  # the draft's source AFTER this /start
    source_rejected: bool  # a payload was given but was not a valid source


def _identity_update(profile: BotProfile, telegram_user_id: int, telegram_chat_id: int, telegram_username: str | None) -> dict:
    return {
        "country_code": profile.country_code,
        "channel": "telegram",
        "bot_key": profile.bot_key,
        "telegram_user_id": telegram_user_id,
        "telegram_chat_id": telegram_chat_id,
        "telegram_username": telegram_username,
        # Fixed policyholder contacts of this bot (if configured) -- the
        # customer is never asked for them (see BotProfile.fixed_contacts).
        **profile.fixed_contacts(),
    }


def ensure_customer_session(
    conn: sqlite3.Connection,
    profile: BotProfile,
    *,
    telegram_user_id: int,
    telegram_chat_id: int,
    telegram_username: str | None,
) -> str:
    """Session row + bot/customer identity on the draft, for any update --
    including a button pressed without a /start since the draft was last
    cleared. Never touches acquisition_source (only /start does)."""
    session_id = session_id_for(profile.bot_key, telegram_user_id)
    ensure_session(conn, session_id)
    merge_draft(conn, session_id, _identity_update(profile, telegram_user_id, telegram_chat_id, telegram_username))
    return session_id


def record_start(
    conn: sqlite3.Connection,
    profile: BotProfile,
    *,
    telegram_user_id: int,
    telegram_chat_id: int,
    telegram_username: str | None,
    payload: str | None,
) -> StartResult:
    session_id = session_id_for(profile.bot_key, telegram_user_id)
    ensure_session(conn, session_id)

    source = parse_source(payload)
    source_rejected = bool(payload and payload.strip()) and source is None

    update = _identity_update(profile, telegram_user_id, telegram_chat_id, telegram_username)
    if source is not None:
        update["acquisition_source"] = source
    draft = merge_draft(conn, session_id, update)

    log_event(
        conn,
        session_id=session_id,
        order_id=None,
        event_name="bot_started",
        properties={"bot_key": profile.bot_key, "source": source, "source_rejected": source_rejected},
    )
    return StartResult(
        session_id=session_id,
        acquisition_source=draft.get("acquisition_source"),
        source_rejected=source_rejected,
    )


def get_customer_draft(conn: sqlite3.Connection, profile: BotProfile, telegram_user_id: int) -> dict:
    return get_draft(conn, session_id_for(profile.bot_key, telegram_user_id)) or {}
